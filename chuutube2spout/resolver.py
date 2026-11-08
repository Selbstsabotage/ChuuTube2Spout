# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn a page URL into a media URL mpv can actually fetch.

Why not just let mpv's ytdl_hook do it: because when YouTube refuses the
resulting URL, mpv reports "failed to load segment" and nothing else. You cannot
see which player client produced it, whether the request carried the headers
that URL was minted for, or which of the dozens of formats was chosen.

Doing it here costs a few hundred milliseconds per load and buys four things:

* the exact HTTP headers travel with the URL, so mpv's request looks like the
  one yt-dlp made - a mismatched User-Agent alone is enough for a 403;
* if a client's URL is refused we find out immediately (we fetch 2 KB of it) and
  transparently try the next client, instead of failing in front of an audience;
* format choice is ours, so "1080p or the next rung up" means exactly that, and
  we can prefer a codec the GPU is happy with;
* failures arrive as sentences a human can act on.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import cookies as cookie_store
from .config import (
    CLIENT_CHAIN, CODEC_PREFERENCE, COOKIE_DOMAINS, COOKIE_EXPORT,
    COOKIE_MAX_AGE_MIN, COOKIES_FROM_BROWSER, PLAYER_CLIENT, VERIFY_STREAMS,
)
from .cookies import resolve as resolve_cookies
from .httpu import build_request, content_length_from_url

VERIFY_BYTES = 2048
# Far enough in to be past any "opening chunk" a server hands out for free.
DEEP_PROBE_AT = 12 * 1024 * 1024
_DIRECT_SCHEMES = ("rtmp://", "rtsp://", "udp://", "srt://", "file://")


@dataclass
class Stream:
    """Everything mpv needs to play one thing."""

    video_url: str
    audio_url: str | None = None
    headers: dict = field(default_factory=dict)
    width: int = 0
    height: int = 0
    codec: str = ""
    client: str = ""
    title: str = ""
    duration: float = 0.0
    is_live: bool = False
    direct: bool = False

    @property
    def label(self) -> str:
        if self.direct:
            if self.client == "local":
                return f"{self.height}p cached" if self.height else "cached"
            return "direct"
        bits = [f"{self.height}p" if self.height else "?"]
        if self.codec:
            bits.append(self.codec)
        if self.client:
            bits.append(f"via {self.client}")
        return " ".join(bits)


class ResolveError(RuntimeError):
    pass


class UnsafeUrl(ValueError):
    """A pasted string that must not be handed to a subprocess."""


def check_url(url: str) -> str:
    """Reject anything a command-line tool would read as an option.

    yt-dlp has `--exec` (runs a command) and `--config-locations` (reads more
    options from a path, including a UNC share). A string like
    `--exec=calc.exe` is a single argv token, so quoting does not help - the
    only defence is refusing to pass it, and passing `--` before the URL so
    nothing after it is parsed as an option either.

    This matters because the URL field is filled by pasting, and a "link"
    handed over in a booking email or a shared setlist is exactly the shape an
    attack takes for a tool like this.
    """
    url = (url or "").strip()
    if not url:
        raise UnsafeUrl("nothing to load")
    if url.startswith("-"):
        raise UnsafeUrl(
            "that starts with '-', so it would be read as a command-line "
            "option rather than a URL. Paste the link itself.")
    if "\n" in url or "\r" in url or "\x00" in url:
        raise UnsafeUrl("a URL cannot contain a line break")
    return url


def is_direct(url: str) -> bool:
    """True for things yt-dlp should not be asked about."""
    lowered = url.lower()
    if os.path.exists(url):
        return True
    if lowered.startswith(_DIRECT_SCHEMES):
        return True
    # A bare media file or stream manifest over http(s).
    return bool(re.search(r"\.(mp4|mkv|webm|mov|avi|flv|ts|m3u8|mpd)(\?|$)", lowered))


def _codec_rank(vcodec: str) -> int:
    """Lower is better. Unlisted codecs sort last but still play."""
    vcodec = (vcodec or "").split(".")[0].lower()
    for i, pref in enumerate(CODEC_PREFERENCE):
        if vcodec.startswith(pref):
            return i
    return len(CODEC_PREFERENCE)


def pick_video(formats: list[dict], max_height: int | None) -> dict | None:
    """Best video at or below max_height; failing that, the smallest above it.

    "Or the next highest" - asking for 480p on a video published only at 720p
    should give you 720p, not an error.
    """
    videos = [f for f in formats
              if f.get("url") and f.get("vcodec") not in (None, "none")]
    if not videos:
        return None
    if max_height is None:
        return max(videos, key=lambda f: ((f.get("height") or 0),
                                          -_codec_rank(f.get("vcodec")),
                                          (f.get("tbr") or 0)))

    fits = [f for f in videos if (f.get("height") or 0) <= max_height]
    if fits:
        return max(fits, key=lambda f: ((f.get("height") or 0),
                                        -_codec_rank(f.get("vcodec")),
                                        (f.get("tbr") or 0)))
    # Nothing small enough - take the least oversized option.
    return min(videos, key=lambda f: ((f.get("height") or 0),
                                      _codec_rank(f.get("vcodec")),
                                      -(f.get("tbr") or 0)))


def pick_audio(formats: list[dict]) -> dict | None:
    audios = [f for f in formats
              if f.get("url")
              and f.get("acodec") not in (None, "none")
              and f.get("vcodec") in (None, "none")]
    if not audios:
        return None
    return max(audios, key=lambda f: (f.get("abr") or f.get("tbr") or 0))


def probe(url: str, headers: dict, start: int, length: int = VERIFY_BYTES,
          timeout: float = 15.0) -> tuple[bool, str]:
    """Fetch `length` bytes at `start`. Returns (ok, detail).

    Built by the same helper the relay uses - a check that does not match the
    real request is worse than no check at all.
    """
    req = build_request(url, headers,
                        {"Range": f"bytes={start}-{start + length - 1}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(length)
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.reason or ''}".strip()
    except Exception as exc:
        return False, str(exc)


def verify(url: str, headers: dict, timeout: float = 15.0) -> tuple[bool, str]:
    """Check the stream is fetchable - at the start AND further in.

    Checking only the first couple of KB is worse than useless here: these
    servers will hand you an opening chunk and then refuse the rest, so a
    start-only check happily certifies a stream that dies 10 MB later. The
    deep probe is what makes the client fallback able to find a client that
    can actually serve a whole file.
    """
    ok, detail = probe(url, headers, 0, VERIFY_BYTES, timeout)
    if not ok:
        return False, detail

    total = content_length_from_url(url)
    if not total or total <= DEEP_PROBE_AT + VERIFY_BYTES:
        return True, detail

    deep_ok, deep_detail = probe(url, headers, DEEP_PROBE_AT, VERIFY_BYTES, timeout)
    if deep_ok:
        return True, detail
    return False, f"{deep_detail} at {DEEP_PROBE_AT // (1 << 20)}MB (start was fine)"


def _client_chain() -> list[str | None]:
    if PLAYER_CLIENT:
        return [PLAYER_CLIENT]
    # None means "don't override - let yt-dlp use its own defaults", which is
    # right most of the time and wrong exactly when YouTube has just changed
    # something. The rest of the chain is the fallback for that day.
    chain: list[str | None] = [None]
    chain += [c for c in CLIENT_CHAIN if c]
    return chain


def cookie_opts() -> dict:
    """Cookie settings for the yt-dlp API, matching downloader.cookie_args().

    The old version handed the raw config value to `cookiesfrombrowser`, which
    only understands yt-dlp's own browser names - so "waterfox" came back as
    `unsupported browser: "waterfox"` on every client in the chain. Nothing
    crashed, resolve() just failed seven times and hybrid mode fell through to
    a plain download every single load, silently giving up the whole point of
    it.

    Preferring our own exported jar also keeps the narrow domain list: the
    browser's entire cookie store is not this app's business.
    """
    if not COOKIES_FROM_BROWSER:
        return {}

    if COOKIE_EXPORT:
        path, warning = cookie_store.ensure(COOKIES_FROM_BROWSER, None,
                                            COOKIE_MAX_AGE_MIN, COOKIE_DOMAINS)
        if warning:
            print(f"[chuutube] cookies: {warning}", file=sys.stderr)
        if path is not None:
            return {"cookiefile": str(path)}

    resolved, warning = resolve_cookies(COOKIES_FROM_BROWSER)
    if warning:
        print(f"[chuutube] cookies: {warning}", file=sys.stderr)
    if not resolved:
        return {}
    # The API wants the parts split out; the CLI takes one "browser:profile".
    browser, _, profile = resolved.partition(":")
    return {"cookiesfrombrowser": (browser, profile or None, None, None)}


class _Silent:
    """A yt-dlp logger that says nothing.

    `quiet` suppresses progress and info but not errors - those go to stderr
    through report_error whatever quiet says. check.bat prints its own tidy
    one-line summary of a failed client, so yt-dlp's raw ERROR block landing
    above it is the same news twice, in the less readable order.
    """

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _extract(url: str, client: str | None, quiet_errors: bool = False) -> dict:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "no_color": True,
    }
    if quiet_errors:
        opts["logger"] = _Silent()
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    opts.update(cookie_opts())
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    if not info:
        raise ResolveError("nothing extracted")
    return info


def resolve(url: str, max_height: int | None,
            progress=None) -> Stream:
    """Resolve `url` to a playable Stream, or raise ResolveError.

    `progress` is an optional callable taking a short status string.
    """
    url = url.strip()
    if not url:
        raise ResolveError("no URL given")

    if is_direct(url):
        return Stream(video_url=url, direct=True, title=os.path.basename(url))

    def say(msg):
        if progress:
            progress(msg)

    attempts: list[str] = []
    for client in _client_chain():
        name = client or "default"
        say(f"resolving ({name})...")
        try:
            info = _extract(url, client)
        except Exception as exc:
            attempts.append(f"{name}: {_short(exc)}")
            continue

        formats = info.get("formats") or []
        if not formats and info.get("url"):
            # Single-URL result (many non-YouTube sites, and live HLS).
            stream = Stream(video_url=info["url"],
                            headers=dict(info.get("http_headers") or {}),
                            width=info.get("width") or 0,
                            height=info.get("height") or 0,
                            client=name,
                            title=info.get("title") or "",
                            duration=float(info.get("duration") or 0.0),
                            is_live=bool(info.get("is_live")))
            if not VERIFY_STREAMS:
                return stream
            ok, detail = verify(stream.video_url, stream.headers)
            if ok:
                return stream
            attempts.append(f"{name}: {detail}")
            continue

        video = pick_video(formats, max_height)
        if not video:
            attempts.append(f"{name}: no video formats")
            continue

        headers = dict(video.get("http_headers") or info.get("http_headers") or {})
        audio = None
        if video.get("acodec") in (None, "none"):
            audio_fmt = pick_audio(formats)
            audio = audio_fmt.get("url") if audio_fmt else None

        stream = Stream(
            video_url=video["url"],
            audio_url=audio,
            headers=headers,
            width=video.get("width") or 0,
            height=video.get("height") or 0,
            codec=(video.get("vcodec") or "").split(".")[0],
            client=name,
            title=info.get("title") or "",
            duration=float(info.get("duration") or 0.0),
            is_live=bool(info.get("is_live")),
        )

        if not VERIFY_STREAMS:
            return stream

        say(f"checking ({name})...")
        ok, detail = verify(stream.video_url, headers)
        if ok:
            return stream
        attempts.append(f"{name}: {detail}")

    raise ResolveError(_summarise(attempts))


def _short(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ").strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    return text[:110] or exc.__class__.__name__


def _summarise(attempts: list[str]) -> str:
    if not attempts:
        return "could not resolve"
    deep = [a for a in attempts if "start was fine" in a]
    if deep and len(deep) == len(attempts):
        return ("every client serves the opening chunk then refuses the rest. "
                "That is YouTube's PO-token gate - sign in with "
                "CHUUTUBE2SPOUT_COOKIES_FROM_BROWSER=firefox, or see the README.")
    forbidden = [a for a in attempts if "403" in a]
    if len(forbidden) == len(attempts):
        return ("every client refused (403). YouTube is blocking this IP for "
                "media, not your setup - try without a VPN, or set "
                "CHUUTUBE2SPOUT_COOKIES_FROM_BROWSER to sign in.")
    return "; ".join(attempts[:3])
