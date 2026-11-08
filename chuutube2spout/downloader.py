# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch a clip to disk by running yt-dlp, then play it locally.

Streaming these URLs directly does not work, and not for want of trying. A
signed googlevideo URL tolerates a handful of requests and then answers 403 to
everything - not by offset, not by window size, by *count*. A player opening a
file makes far more requests than that, and every retry spends the allowance
faster. yt-dlp copes by re-extracting and resuming, so it does the fetching.

It is run as a **subprocess**, not through its Python API, deliberately:

* a subprocess inherits PATH, so it finds the same deno, the same plugins and
  the same cookies as the copy you ran by hand;
* it is passed --ignore-config, so your personal yt-dlp config file cannot
  change what the app does behind its back. That reverses an earlier decision:
  the config used to be honoured so "it works standalone" and "it works from
  the app" stayed the same test. In practice it stopped being one - a config
  carrying --cookies-from-browser re-read the browser's entire cookie store
  (1267 cookies) on top of the 110-cookie jar this app deliberately exports,
  quietly undoing the narrowing in cookies.py. A tester's stray config also
  changes behaviour nobody can see while debugging;
* everything it prints is forwarded to the console instead of being swallowed,
  which is how a missing JavaScript runtime becomes visible rather than
  mysterious.

Video and audio are kept as separate files on purpose: merging them would need
ffmpeg, and mpv plays a video with an external audio track quite happily. They
are however fetched in ONE invocation (`-f "video,bestaudio"` - a comma means
"download both, do not merge"), because a second invocation meant extracting the
page again, minting a second PO token and sitting through yt-dlp's politeness
sleeps a second time. That was most of the wait before a clip appeared.

A successful fetch also leaves a small manifest in the cache, so loading the same
URL at the same quality again skips yt-dlp entirely rather than paying for a full
extraction just to be told the file is already there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from typing import NamedTuple
from pathlib import Path

from .bootstrap import ytdlp_command
from .config import (AUTO_CLIENT, CACHE_DIR, CACHE_MANIFEST, CACHE_MAX_GB, CACHE_MIN_AGE_MIN,
                     CLIENT_CHAIN, COOKIE_DOMAINS,
                     COOKIE_EXPORT, COOKIE_MAX_AGE_MIN, COOKIES_FROM_BROWSER,
                     HTTP_CHUNK_SIZE, PLAYER_CLIENT, REMOTE_COMPONENTS,
                     CACHE_MAX_AGE_H, FETCH_AUDIO, INFO_CACHE, INFO_CACHE_MIN,
                     LOAD_TIMING,
                     PARTIAL_AFTER_S, PARTIAL_PLAY,
                     PARTIAL_START_MB,
                     SINGLE_PASS_AUDIO, YTDLP_VERBOSE)
from . import cookies as cookie_store
from .cookies import resolve as resolve_cookies
from .redact import scrub
from .potprovider import get_provider

FIELD_SEP = "\x1f"
PRINT_TEMPLATE = ("after_move:%(filepath)s" + FIELD_SEP + "%(height)s"
                  + FIELD_SEP + "%(width)s" + FIELD_SEP + "%(title)s"
                  + FIELD_SEP + "%(acodec)s" + FIELD_SEP + "%(vcodec)s")

MANIFEST_VERSION = 2
_NO_FORMAT = re.compile(r"requested format is not available", re.I)

# YouTube's answer to a request whose headers are too big. In practice
# that means the Cookie header, so it fails identically for every client -
# there is no point working through the chain with the same jar.
_TOO_LARGE = re.compile(r"HTTP Error 413")

# yt-dlp found a file of the same name and asked YouTube for the rest of it -
# and YouTube answered that there is no rest. With --no-part the file under
# the final name IS the partial, so a clip that finished downloading earlier
# looks like an unfinished one (tester, 0.9.9.35: "Resuming download at byte
# 9719397", the full size, then 416).
_RESUMING = re.compile(r"\[download\] Resuming download at byte")
_RANGE_PAST_END = re.compile(r"HTTP Error 416")

# YouTube's bot check. yt-dlp prints it with a typographic apostrophe
# ("you’re"), hence the dot.
_BOT_CHECK = re.compile(r"Sign in to confirm you.re not a bot", re.I)

# What the slot's status line says when that happens without a login. Short,
# because the status line is narrow; the README has the steps.
SIGN_IN_HINT = ("YouTube asks for a sign-in - set COOKIES_FROM_BROWSER in "
                "settings.txt (README: Signing in to YouTube)")

# "[download]  45.2% of   60.75MiB at    8.00MiB/s ETA 00:02"
_PROGRESS = re.compile(r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\w+)"
                       r"(?:\s+at\s+(\S+))?")
_ALREADY = re.compile(r"\[download\].*has already been downloaded")


@dataclass
class LocalClip:
    video_path: str
    audio_path: str | None = None
    title: str = ""
    width: int = 0
    height: int = 0
    acodec: str = ""

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec) and self.acodec not in ("none", "NA")

    @property
    def label(self) -> str:
        return f"{self.height}p cached" if self.height else "cached"


@dataclass
class Entry:
    """One file yt-dlp actually wrote, as reported by --print-to-file."""
    path: str
    height: int = 0
    width: int = 0
    title: str = ""
    acodec: str = ""
    vcodec: str = ""

    @property
    def is_video(self) -> bool:
        return self.height > 0 or (self.vcodec not in ("", "none", "NA"))


class DownloadError(RuntimeError):
    pass


class SignInRequired(DownloadError):
    """YouTube wants a login, and no cookies were sent."""


class FormatUnavailable(DownloadError):
    """The format *string* was wrong for this video - not the client."""


class DownloadCancelled(Exception):
    """We stopped it on purpose. Deliberately not a DownloadError.

    A prefetch that is abandoned because the URL changed has not failed, and
    must not reach the status line as though it had.
    """


# Resolution first, codec second. yt-dlp applies this to every branch of the
# -f expression, so "best" means "tallest, and among equals the one this
# machine decodes most cheaply".
#
# H.264 is still the preference - nvdec handles it on everything - but it is a
# TIE-BREAK now, not a veto. It used to live in the -f expression as
# `[vcodec^=avc1]` in the leading branch, which quietly meant "H.264 at any
# height beats anything else at any height". A video whose avc1 ladder stops at
# 480p played at 480p while 1080p VP9 and AV1 sat in the same format list -
# which on a projector is not a subtle failure.
#
# resolver.pick_video has always sorted this way. This is the download path
# being taught the same rule rather than the two disagreeing.
FORMAT_SORT = "res,vcodec:h264"

# <title> [<id>].f<format id>.<ext>
#
# The id stays because it is what makes a name unique and greppable - two
# uploads share a title far more often than they share an id - and the format
# id because the same video at two qualities has to be two files.
#
# `.80s` is a hard truncation, and it is about MAX_PATH rather than tidiness:
# Windows still refuses paths past 260 characters for most callers, and a cache
# folder a few directories deep plus a full title gets there. It counts
# characters rather than bytes, which is what both Python and NTFS count, so a
# Japanese title is 80 glyphs and not 27.
#
# --windows-filenames is what removes < > : " / \ | ? *, trailing dots and
# spaces, and the reserved device names (CON, NUL, COM1...). It is the default
# when running ON Windows; passing it explicitly means a build run from
# anywhere behaves the same.
#
# NOT --restrict-filenames. That flattens non-ASCII, which would turn every
# Japanese title into a row of underscores and leave a folder of clips nobody
# can tell apart. Half this library is Japanese.
OUT_TEMPLATE = "%(title).80s [%(id)s].f%(format_id)s.%(ext)s"


def video_format(max_height: int | None) -> str:
    """Video-only, capped at max_height, with a step up if nothing fits.

    Codec preference is not expressed here - see FORMAT_SORT, which orders the
    candidates inside whatever these branches select.

    The trailing unrestricted alternative is what makes "or the next highest"
    true: asking for 480p on a video published only at 720p gives you 720p
    rather than an error.
    """
    if max_height is None:
        return "bestvideo/best"
    return (f"bestvideo[height<=?{max_height}]/"
            f"best[height<=?{max_height}]/"
            f"bestvideo/best")


def js_runtime_arg() -> list[str]:
    """Point yt-dlp at deno explicitly if we can find it.

    It looks on PATH by itself, but being explicit removes a whole category of
    "is it actually seeing it?" from the diagnosis.
    """
    deno = shutil.which("deno")
    return ["--js-runtimes", f"deno:{deno}"] if deno else []


def parse_progress(line: str) -> str | None:
    """Turn one yt-dlp output line into a status message, or None."""
    if _ALREADY.search(line):
        return "cached"
    match = _PROGRESS.search(line)
    if not match:
        return None
    percent, total, speed = match.groups()
    text = f"downloading {float(percent):.0f}% of {total}"
    return f"{text} at {speed}" if speed else text


_DESTINATION = "[download] Destination: "

# yt-dlp's own switch for the encoding it writes its output in. The environment
# variables in _child_env() do the same job when the child is a normal Python -
# but the release exe re-launches ITSELF as yt-dlp, and the frozen interpreter
# on a tester's machine ran in cp1252 regardless ("Encodings: locale cp1252 ...
# pref cp1252, out cp1252"). yt-dlp encodes its output with errors='ignore', so
# "アザミナ：Robocosan Original Song" reached us as "Robocosan Original
# Song": a Destination path to a file that did not exist, and partial playback
# silently off for every title outside cp1252. This is honoured however the
# child was started, so it does not depend on how PyInstaller treats PYTHON*
# variables.
FORCE_UTF8 = ("--encoding", "utf-8")


def _child_env() -> dict:
    """yt-dlp's environment, forced to speak UTF-8 on stdout.

    Decoding the pipe as UTF-8 at this end is not enough, because the loss
    happens at the far one: on Windows a piped stdout defaults to the ANSI code
    page (yt-dlp says so itself - `out cp1252`), so a filename with characters
    cp1252 cannot spell is mangled BEFORE it reaches us.

    That turned "[download] Destination: ...\u3010ASMR\u3011... \U0001f497\u2b50 ..."
    into a path with the brackets and emoji simply missing. Every byte of the
    download was fine - the file on disk is correctly named, because
    --print-to-file writes UTF-8 - but the path parsed from the log pointed at
    something that did not exist, so partial playback quietly never started.
    It surfaced the moment clips stopped being named after ASCII video ids.

    Not sufficient on its own in the release build - see FORCE_UTF8.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


# hlsnative assembles the file from fragments and then always hands it to
# ffmpeg (FixupM3u8) to be rewritten - see _worth_handing_over.
_FRAGMENTED = re.compile(r"\[hlsnative\]|hlsnative downloader|Total fragments:")


def _big_enough(path: Path) -> bool:
    """Is there enough of this file yet to be worth handing to mpv?

    Deliberately a byte count and not a percentage: what mpv needs is the
    header plus some frames, which is an absolute quantity. A percentage would
    ask for 40 MB of a long clip and 200 KB of a short one, and be wrong both
    times.
    """
    try:
        return path.stat().st_size >= PARTIAL_START_MB * 1024 * 1024
    except OSError:
        return False           # not created yet, or gone again


def _worth_handing_over(path: Path, began: float | None, fragmented: bool) -> bool:
    """Should mpv be given this half-written file, or should it just wait?

    Handing over is not free. mpv holds the file open, and when the download
    lands the file has to be re-opened at the playhead - mpv caches the size at
    open and never notices it growing. Two cases where the cost is not repaid:

    Fragmented downloads. hlsnative finishes by running the whole file through
    ffmpeg (`[FixupM3u8] Fixing MPEG-TS in MP4 container`), which writes a
    .temp.mp4 and renames it over the original. Windows refuses to replace a
    file another process has open, so holding it turns a completed 74 MB
    download into `PermissionError: [WinError 5] Access is denied` and throws
    the whole thing away. Seen in the wild; it cost that load 29 seconds and
    three more client attempts.

    Downloads that are nearly done. The reload lands a couple of seconds after
    the hand-over, and seeking back to 2.5s reads as "it started over" - which
    is precisely the bug this was reported as. If the transfer has not been
    running for PARTIAL_AFTER_S there is nothing to gain: the file would have
    been ready about now anyway. On a venue connection where the transfer takes
    a minute, the hand-over still happens and still saves most of that minute.
    """
    if fragmented:
        return False
    if began is None or time.monotonic() - began < PARTIAL_AFTER_S:
        return False
    return _big_enough(path)


class Attempt(NamedTuple):
    """One run of yt-dlp, and the two allowances that make it different.

    `allow_hls` is off except on the deferred retry (see _defer_hls); `cookies`
    is on until YouTube refuses the request as too large.
    """
    label: str
    extra: list[str]
    allow_hls: bool = False
    cookies: bool = True


def _command(ytdlp, url: str, fmt: str, cache: Path,
             print_file: Path, extra: list[str],
             cookies: bool = True) -> list[str]:
    # `ytdlp` is an argv prefix, not a path: it may be ["yt-dlp.exe"] or
    # [python, "-m", "yt_dlp"] when the folder has been moved and the console
    # script's baked-in path no longer resolves.
    prefix = [ytdlp] if isinstance(ytdlp, str) else list(ytdlp)
    cmd = [
        *prefix, "--newline", "--no-playlist", "--no-simulate",
        "--no-color", "--progress", *FORCE_UTF8,
        "-f", fmt, "-S", FORMAT_SORT,
        "-o", str(cache / OUT_TEMPLATE), "--windows-filenames",
        "--print-to-file", PRINT_TEMPLATE, str(print_file),
        "--retries", "10", "--fragment-retries", "10",
        # Your ~/yt-dlp/config.txt is NOT read. See the module docstring: one
        # carrying --cookies-from-browser silently widened the cookie jar from
        # 110 YouTube cookies to the browser's entire store.
        "--ignore-config",
    ]
    if PARTIAL_PLAY:
        # Write straight to the destination instead of to a .part file that is
        # renamed at the end. Windows refuses to rename a file another process
        # has open, and the process holding it open would be our own mpv - so
        # partial playback and the .part rename cannot coexist.
        #
        # --no-overwrites goes with it. Together they would treat a half-written
        # file from an interrupted run as "already downloaded" and skip it,
        # leaving a truncated clip that never repairs itself. Re-fetching is
        # decided by the manifest, which is only written on success.
        cmd.append("--no-part")
    else:
        cmd.append("--no-overwrites")
    if REMOTE_COMPONENTS:
        # Without this, signature solving fails and the only formats left are
        # the PO-token-gated ones - which is the whole problem.
        cmd += ["--remote-components", REMOTE_COMPONENTS]
    if HTTP_CHUNK_SIZE:
        cmd += ["--http-chunk-size", HTTP_CHUNK_SIZE]
    if INFO_CACHE:
        # Written on the way past, so the *next* load of this URL - at any
        # quality - can skip everything above.
        cmd += ["--write-info-json",
                "-o", f"infojson:{info_stem(cache, url)}"]
    cmd += cookie_args() if cookies else []
    cmd += get_provider().extractor_args()
    if YTDLP_VERBOSE:
        cmd.append("-v")
    cmd += extra
    # "--" ends option parsing. Without it, a pasted string beginning with a
    # dash is read by yt-dlp as one of its own options, and yt-dlp has options
    # that run commands (--exec) and options that load more options from a path
    # (--config-locations). One pasted token would then be arbitrary code
    # execution on the machine driving the show. check_url() rejects such
    # strings before we get here; this is the second lock on the same door.
    cmd += ["--", url]
    return cmd


def _info_command(ytdlp, info: Path, fmt: str, cache: Path,
                  print_file: Path) -> list[str]:
    """Download from a cached description instead of extracting again.

    Everything to do with reaching YouTube is gone: no client, no cookies, no
    PO token, no JS challenge. The formats were listed when the description was
    written, so -f picks from those - which is the whole trick behind changing
    quality without paying for a second extraction.
    """
    prefix = [ytdlp] if isinstance(ytdlp, str) else list(ytdlp)
    cmd = [
        *prefix, "--newline", "--no-playlist", "--no-simulate", "--no-color",
        "--progress", *FORCE_UTF8, "-f", fmt, "-S", FORMAT_SORT,
        "-o", str(cache / OUT_TEMPLATE), "--windows-filenames",
        "--print-to-file", PRINT_TEMPLATE, str(print_file),
        "--retries", "10", "--fragment-retries", "10",
        "--ignore-config",
    ]
    if PARTIAL_PLAY:
        cmd.append("--no-part")
    else:
        cmd.append("--no-overwrites")
    if HTTP_CHUNK_SIZE:
        cmd += ["--http-chunk-size", HTTP_CHUNK_SIZE]
    if YTDLP_VERBOSE:
        cmd.append("-v")
    cmd += ["--load-info-json", str(info)]
    return cmd


def cookie_args() -> list[str]:
    """How yt-dlp is told about cookies, preferring our own snapshot.

    `--cookies-from-browser` reads the live store on every invocation, which
    both costs time and wants the browser closed. Exporting a snapshot once
    means Waterfox can stay open - which it has to, since that is where the URLs
    are being copied from.
    """
    if not COOKIES_FROM_BROWSER:
        return []

    if COOKIE_EXPORT:
        # No cache_dir: the jar goes to a private per-process temp directory and
        # is deleted on exit, so no copy or zip of the app folder carries a live
        # login out with it.
        path, warning = cookie_store.ensure(COOKIES_FROM_BROWSER, None,
                                            COOKIE_MAX_AGE_MIN, COOKIE_DOMAINS)
        if warning:
            print(f"[chuutube] cookies: {warning}", file=sys.stderr)
        if path is not None:
            print(f"[chuutube] cookies: using {path}", file=sys.stderr)
            return ["--cookies", str(path)]

    # Chrome-family stores are encrypted, so this is the only route for them.
    resolved, warning = resolve_cookies(COOKIES_FROM_BROWSER)
    if resolved:
        return ["--cookies-from-browser", resolved]
    if warning:
        print(f"[chuutube] cookies: {warning}", file=sys.stderr)
    return []


def client_arg(client: str | None) -> list[str]:
    return ["--extractor-args", f"youtube:player_client={client}"] if client else []


def _strategies() -> list[tuple[str, list[str]]]:
    """(label, extra args) to try in order until one produces a file.

    YouTube keeps moving which clients are usable: some are SABR-only, some
    need a PO token, some need signatures that only solve with a JS runtime.
    Rather than encode today's answer, try them and keep the one that works.
    """
    js = js_runtime_arg()
    if PLAYER_CLIENT:
        clients: list[str | None] = [PLAYER_CLIENT]
    else:
        # "default" is an ordinary entry in the chain now, so it can be first -
        # which it is, because it is the one that keeps working. It used to be
        # appended here unconditionally, which meant the fastest client was
        # always tried last, after four slower ways of failing.
        clients = [None if c.lower() in AUTO_CLIENT else c
                   for c in CLIENT_CHAIN[:5]]
        if None not in clients:
            clients.append(None)   # always leave yt-dlp's own choice as a floor

    out = [(c or "default", js + client_arg(c)) for c in clients]
    if js:
        # An older yt-dlp may not know --js-runtimes at all.
        out.append(("no js flag", client_arg(clients[0])))
    # Last resort: ignore any partial file and start clean.
    out.append(("fresh start", js + client_arg(clients[0]) + ["--no-continue"]))
    return out




# --- where the time actually goes --------------------------------------------
# Measured on three loads, the download is the cheap half: 2-7s of transfer
# against a flat ~11-12s before a single byte of video is fetched, near enough
# independent of file size. That overhead is spread across steps that happen
# inside yt-dlp, so the only way to see it from out here is to watch what it
# says and stamp the clock as each phase announces itself.
_PHASE_MARKERS = (
    # (phase this line STARTS, substring to look for)
    ("extract", "Downloading webpage"),
    ("token", "Generating a gvs PO Token"),
    ("challenge", "Solving JS challenges"),
    ("player", "Downloading player "),
    ("transfer", "[download] Destination:"),
)
_SLEEP_RE = re.compile(r"Sleeping ([\d.]+) seconds")


class PhaseTimer:
    """Attribute wall-clock time to the phases yt-dlp narrates.

    Deliberately marker-driven rather than a guess at what is slow: the phases
    are whatever yt-dlp announces, and a phase that never announces itself
    simply does not appear. Throttle sleeps are counted separately - they are
    not our overhead to optimise, and lumping them in would flatter or damn the
    step that happened to contain them.
    """

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.marks: list[tuple[str, float]] = []
        # (when it was announced, how long) - the sleep is charged to whichever
        # phase was open at the time, and taken back off it.
        self.sleeps: list[tuple[float, float]] = []
        self._phase: str | None = None

    @property
    def slept(self) -> float:
        return sum(seconds for _, seconds in self.sleeps)

    def line(self, text: str) -> None:
        found = _SLEEP_RE.search(text)
        if found:
            try:
                self.sleeps.append((time.monotonic(), float(found.group(1))))
            except ValueError:
                pass
        for phase, marker in _PHASE_MARKERS:
            if marker in text and phase != self._phase:
                self.marks.append((phase, time.monotonic()))
                self._phase = phase
                break

    def _slept_between(self, start: float, end: float) -> float:
        return sum(s for at, s in self.sleeps if start <= at < end)

    def summary(self) -> str:
        end = time.monotonic()
        parts = []
        for index, (phase, at) in enumerate(self.marks):
            until = self.marks[index + 1][1] if index + 1 < len(self.marks) else end
            # Waiting because YouTube said to is not this phase being slow. The
            # first version of this reported "challenge 5.1s" for a step that
            # took 1.1s and happened to straddle a 4s throttle - which is
            # exactly the sort of number that sends someone optimising the
            # wrong thing.
            # Clamped: yt-dlp announces the sleep before taking it, so a run
            # killed mid-sleep can subtract more than actually elapsed.
            spent = max(0.0, until - at - self._slept_between(at, until))
            parts.append(f"{phase} {spent:.1f}s")
        # Time before the first marker is yt-dlp starting up: interpreter,
        # 1744 extractors, plugin discovery. It is not nothing.
        if self.marks:
            parts.insert(0, f"startup {self.marks[0][1] - self.started:.1f}s")
        if self.slept:
            parts.append(f"throttle {self.slept:.1f}s")
        parts.append(f"total {end - self.started:.1f}s")
        return " | ".join(parts)


def _defer_hls(attempts: list, deferred: tuple[str, list[str]]) -> None:
    """Queue the HLS-only client again, this time allowed to go through with it.

    Appending to the list being iterated is deliberate: `enumerate` over a list
    picks the new entry up, and `len(attempts)` is re-read each time round, so
    the "last attempt" test stays correct.
    """
    label, extra = deferred
    attempts.append(Attempt(f"{label} (HLS fallback)", extra, allow_hls=True))


def _run(ytdlp: str, url: str, fmt: str, cache: Path, progress,
         bail_on_format_error: bool = False, on_partial=None,
         on_partial_lost=None, cancel=None, info: Path | None = None) -> list[Entry]:
    """Run yt-dlp once per strategy until one works. Returns what it wrote.

    With `info`, there is no strategy to choose: the description already says
    which formats exist, so there is exactly one attempt and no client chain to
    work through. Everything else - progress, partial playback, cancellation,
    the timing line - is the same, which is why this shares the loop rather
    than having a second copy of it.
    """
    handle, print_path = tempfile.mkstemp(prefix="chuutube-", suffix=".txt")
    os.close(handle)
    print_file = Path(print_path)

    # Third element: may this attempt settle for HLS? Normally no - see the
    # hls_only handling below - but the deferred retry appended at the end of
    # the chain exists precisely to accept it.
    if info is not None:
        # allow_hls, because nothing here can fall back to a manifest: the
        # formats are fixed and already chosen.
        attempts = [Attempt("cached description", [], allow_hls=True)]
    else:
        attempts = [Attempt(label, extra) for label, extra in _strategies()]
    # The first client that had the video but only as HLS, kept aside in case
    # nothing better turns up.
    hls_deferred: tuple[str, list[str]] | None = None

    tail: list[str] = []
    try:
        for index, attempt in enumerate(attempts):
            label_extra, extra, allow_hls = (attempt.label, attempt.extra,
                                             attempt.allow_hls)
            if progress:
                progress(f"fetching ({label_extra})...")
            print(f"[chuutube] trying: {label_extra}", file=sys.stderr)
            cmd = (_info_command(ytdlp, info, fmt, cache, print_file)
                   if info is not None
                   else _command(ytdlp, url, fmt, cache, print_file, extra,
                                 cookies=attempt.cookies))
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=_child_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            tail = []
            unknown_option = False
            no_format = False
            too_large = False
            bot_check = False
            resumed = False
            past_end = False
            timer = PhaseTimer()
            # Where yt-dlp says it is writing, and whether we have handed it to
            # mpv yet. Checked on progress lines rather than by a timer: they
            # arrive often enough and only while there is something to watch.
            destination: Path | None = None
            handed_over = False
            # When the bytes actually started moving, and whether yt-dlp is
            # assembling this one out of fragments. Both decide whether handing
            # the file to mpv is a good idea at all - see _worth_handing_over.
            transfer_began: float | None = None
            fragmented = False
            hls_only = False
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                # Everything yt-dlp says reaches the console. Suppressing this
                # is how a missing JS runtime stayed invisible for a week - but
                # at -v it also prints the user's IP, the PO token and the URL
                # signature, and console logs get pasted into bug reports.
                # Redacting here covers the derived error messages too, because
                # `tail` is what _explain() quotes back to the user.
                if cancel is not None and cancel.is_set():
                    # Checked per line rather than per attempt: a prefetch of a
                    # 70 MB clip is most of a minute, and the whole point of
                    # abandoning it is to stop it competing for the link with
                    # the clip actually being loaded.
                    proc.terminate()
                    raise DownloadCancelled(url)
                line = scrub(line)
                timer.line(line)
                print(f"[yt-dlp] {line}", file=sys.stderr)
                tail.append(line)
                del tail[:-40]
                if "no such option" in line.lower() and "js-runtime" in line.lower():
                    unknown_option = True
                if _NO_FORMAT.search(line):
                    no_format = True
                if _TOO_LARGE.search(line):
                    too_large = True
                if _BOT_CHECK.search(line):
                    bot_check = True
                if _RESUMING.search(line):
                    resumed = True
                if _RANGE_PAST_END.search(line):
                    past_end = True
                if _FRAGMENTED.search(line):
                    fragmented = True
                if fragmented and destination is None and not allow_hls:
                    # This client has the video but is only offering HLS - it
                    # went SABR-only, so yt-dlp fell back to the m3u8 manifest.
                    # That is a bad deal: fragments come with a throttle
                    # ("Sleeping 2.00 seconds as required by the site"), the
                    # muxed HLS itags cap out lower than the progressive ones
                    # (one load came back 480p when 1080p existed), and the
                    # closing ffmpeg fixup rules out playing while it
                    # downloads. Stop before a single byte moves and see what
                    # the rest of the chain offers; this one is kept as a
                    # fallback if nothing better turns up.
                    hls_only = True
                    proc.terminate()
                    break
                if line.startswith(_DESTINATION):
                    destination = Path(line[len(_DESTINATION):].strip())
                    if not destination.parent.is_dir():
                        # Never silently. A destination we cannot find means no
                        # partial playback and no explanation, which is how the
                        # encoding bug above hid for a release.
                        print(f"[chuutube] cannot see the destination folder "
                              f"({destination}) - no early playback",
                              file=sys.stderr)
                    transfer_began = time.monotonic()
                if on_partial and destination is not None and not handed_over:
                    if _worth_handing_over(destination, transfer_began, fragmented):
                        # A hook that returns False has nobody to play it yet -
                        # a prefetch no Load has joined - and is asked again on
                        # the next line. Announcing "playing while it
                        # downloads" for a file nothing had opened was the
                        # other way to get this wrong.
                        try:
                            taken = on_partial(str(destination)) is not False
                        except Exception as exc:      # never kill the download
                            taken = True              # and never ask it again
                            print(f"[chuutube] could not start early: {exc}",
                                  file=sys.stderr)
                        if taken:
                            handed_over = True
                            print(f"[chuutube] playing while it downloads: "
                                  f"{destination}", file=sys.stderr)
                if progress:
                    message = parse_progress(line)
                    if message:
                        progress(message)
            proc.wait()
            if handed_over and proc.returncode != 0 and on_partial_lost:
                # mpv is playing a file this attempt abandoned half-written,
                # and the next strategy may well choose the same format - the
                # output template is keyed on format id, so it would write into
                # the very file mpv has open, either resuming from the middle
                # or (on "fresh start") rewriting from byte zero underneath a
                # running decoder. Make it let go, and wait until it has.
                try:
                    on_partial_lost()
                except Exception as exc:
                    print(f"[chuutube] could not release the partial file: "
                          f"{exc}", file=sys.stderr)
            if LOAD_TIMING and not hls_only:
                # An attempt we stopped before the transfer has no phases worth
                # reporting - "total 0.0s" next to a real breakdown just reads
                # as something having gone wrong.
                print(f"[chuutube] timing: {timer.summary()}", file=sys.stderr)
            try:
                proc.stdout.close()      # a set is long; do not leak pipes
            except Exception:
                pass

            if unknown_option and extra:
                continue             # retry without the flag
            if proc.returncode == 0:
                break
            if too_large and attempt.cookies:
                # Not this client's fault, and not worth discovering four more
                # times: the jar goes on every request, so every remaining
                # attempt would 413 the same way. Retry this one immediately
                # without it - most videos do not need a login at all.
                print("[chuutube] YouTube refused the request as too large - "
                      "the cookie jar is oversized. Retrying without cookies; "
                      "clear your browser's youtube.com cookies and sign in "
                      "again to fix it properly.", file=sys.stderr)
                # Replaces the tail rather than inserting into it: the
                # cookie-bearing versions of these are now known-dead, and
                # leaving them queued would 413 again and re-trigger this.
                attempts[index + 1:] = [
                    a._replace(label=f"{a.label} (no cookies)", cookies=False)
                    for a in attempts[index:]]
                continue
            if (resumed and past_end and proc.returncode != 0
                    and "--no-continue" not in attempt.extra):
                # The leftover file, not the client, is what failed: every
                # other client would resume the same file into the same 416.
                # Start this client over from byte zero, now, instead of
                # walking the chain - which took 19 s in the tester's log and
                # once ended on a different client at 240p.
                print("[chuutube] a file of this clip was already in the "
                      "cache and could not be resumed - downloading it again "
                      "from the start", file=sys.stderr)
                attempts.insert(index + 1, attempt._replace(
                    label=f"{label_extra} (from the start)",
                    extra=list(attempt.extra) + ["--no-continue"]))
                continue
            if bot_check and not (attempt.cookies and COOKIES_FROM_BROWSER):
                # K, 0.9.9.31: without cookies every client got the same
                # answer, plus HTTP 429 - and the chain has up to seven
                # entries. The check is about this computer's address, not
                # the client, so the rest would only add requests against
                # the rate limit. With cookies, the chain carries on as
                # before: that case has not been seen to fail every client.
                print(f"[chuutube] {SIGN_IN_HINT}. Not trying the other "
                      f"clients: without a login they get the same answer.",
                      file=sys.stderr)
                raise SignInRequired(SIGN_IN_HINT)
            if hls_only:
                # Not a failure - we stopped it - so it must not be reported as
                # one, and it must not consume the "last attempt" case below.
                print(f"[chuutube] {label_extra}: only HLS on offer, "
                      f"trying the rest of the chain first", file=sys.stderr)
                if hls_deferred is None:
                    hls_deferred = (label_extra, extra)
                if index >= len(attempts) - 1:
                    _defer_hls(attempts, hls_deferred)
                    hls_deferred = None
                continue
            if no_format and bail_on_format_error:
                # Every client offers the same *kinds* of format, so working
                # through the rest of the chain would just be five more slow
                # ways of being told the same thing. The caller retries with a
                # simpler format string instead.
                raise FormatUnavailable(_explain(tail, proc.returncode))
            if index < len(attempts) - 1:
                continue             # fall through to the next strategy
            if hls_deferred is not None:
                # Everything else failed, so the HLS one we walked away from is
                # now the best offer on the table. Cleared first: without that
                # this would queue itself again on the retry's own failure.
                _defer_hls(attempts, hls_deferred)
                hls_deferred = None
                continue
            raise DownloadError(_explain(tail, proc.returncode))

        return _read_entries(print_file)
    finally:
        print_file.unlink(missing_ok=True)


def _read_entries(print_file: Path) -> list[Entry]:
    """Parse --print-to-file output: one line per file yt-dlp produced."""
    try:
        raw = print_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    entries: list[Entry] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(FIELD_SEP)
        path = parts[0].strip()
        if not path or path in seen or not Path(path).is_file():
            continue
        seen.add(path)

        def field(index: int) -> str:
            return parts[index].strip() if len(parts) > index else ""

        def as_int(index: int) -> int:
            try:
                return int(field(index))
            except ValueError:
                return 0

        entries.append(Entry(path=path, height=as_int(1), width=as_int(2),
                             title=field(3), acodec=field(4), vcodec=field(5)))
    return entries


def _to_clip(entries: list[Entry]) -> LocalClip | None:
    """Fold what yt-dlp wrote into one clip, video first, audio alongside."""
    videos = [e for e in entries if e.is_video]
    audios = [e for e in entries if not e.is_video]
    if not videos:
        return None
    best = max(videos, key=lambda e: (e.height, e.width))
    clip = LocalClip(video_path=best.path, height=best.height, width=best.width,
                     title=best.title, acodec=best.acodec)
    # A muxed format already carries sound; adding an external track on top of
    # it would give mpv two audio streams to choose between.
    if audios and not clip.has_audio:
        clip.audio_path = audios[0].path
    return clip


AUDIO_EXTS = (".m4a", ".webm", ".opus", ".mp3", ".aac", ".ogg", ".mp4a")


def find_sibling_audio(video_path: str) -> str | None:
    """Locate the audio file of a two-file fetch by name, as a backstop.

    yt-dlp names both files `<id>.f<format>.<ext>`, so the audio track of a
    given video is sitting right next to it. This only matters if the per-file
    print hook did not report the second download.
    """
    video = Path(video_path)
    stem = video.name.split(".f", 1)[0]
    if not stem:
        return None
    best = None
    for candidate in video.parent.glob(f"{stem}.f*"):
        if candidate == video or not candidate.is_file():
            continue
        if candidate.suffix.lower() in AUDIO_EXTS:
            if best is None or candidate.stat().st_mtime > best.stat().st_mtime:
                best = candidate
    return str(best) if best else None


# --- cache manifest ----------------------------------------------------------

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_PATH = re.compile(r"^/(?:shorts|embed|live|v)/([^/?#]+)")


def cache_key(url: str) -> str:
    """What names the video in a link, for the cache.

    The same YouTube video arrives as `watch?v=ID`, `watch?v=ID&pp=...`,
    `youtu.be/ID?si=...` or `shorts/ID`. Keyed on the whole link, each of those
    missed the clip already in the cache (tester, 0.9.9.35): yt-dlp was run
    again, found the finished file, tried to resume it past its end and got
    HTTP 416 from every client in the chain. Anything that is not a
    recognisable YouTube link keys on the link as before.
    """
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    video = None
    if host == "youtu.be":
        video = parts.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com") \
            or host.endswith(".youtube.com") \
            or host.endswith(".youtube-nocookie.com"):
        if parts.path.rstrip("/") == "/watch":
            video = urllib.parse.parse_qs(parts.query).get("v", [None])[0]
        else:
            found = _YOUTUBE_PATH.match(parts.path)
            video = found.group(1) if found else None
    if video and _YOUTUBE_ID.match(video):
        return f"youtube:{video}"
    return url


def manifest_path(cache: Path, url: str, max_height: int | None) -> Path:
    key = hashlib.sha1(f"{cache_key(url)}\x1f{max_height}".encode("utf-8")
                       ).hexdigest()[:16]
    return cache / f".manifest-{key}.json"


def info_stem(cache: Path, url: str) -> Path:
    """What to hand yt-dlp as the info-json output template.

    yt-dlp appends `.info.json` to whatever this names, so it must be the bare
    stem. Passing the full filename produced `.info-<key>.info.info.json`,
    which fresh_info() then never found - the cache was being written
    diligently and read never.
    """
    key = hashlib.sha1(cache_key(url).encode("utf-8")).hexdigest()[:16]
    return cache / f".info-{key}"


def info_path(cache: Path, url: str) -> Path:
    """Where yt-dlp's description of this video is kept.

    Keyed on the URL alone, deliberately: the whole point is that it is reused
    across qualities. It lists every format, so asking for a different one is a
    matter of picking again from an answer we already have.
    """
    return info_stem(cache, url).with_name(info_stem(cache, url).name
                                           + ".info.json")


def fresh_info(cache: Path, url: str) -> Path | None:
    """A cached description recent enough to be worth replaying, if any.

    The media URLs in it are signed and time-limited, so this is a window, not
    a cache in the usual sense. Being wrong is cheap - a failed attempt and a
    normal fetch - but only if the window is short enough that it is rare.
    """
    if not INFO_CACHE:
        return None
    path = info_path(cache, url)
    try:
        age = (time.time() - path.stat().st_mtime) / 60.0
    except OSError:
        return None
    return path if age < INFO_CACHE_MIN else None


def read_manifest(path: Path) -> LocalClip | None:
    """A previous fetch of this exact URL + quality, if the files still exist."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("version") != MANIFEST_VERSION:
        return None
    video = data.get("video_path") or ""
    if not video or not Path(video).is_file():
        return None
    audio = data.get("audio_path") or None
    if audio and not Path(audio).is_file():
        audio = None            # video alone still beats re-downloading both
    return LocalClip(video_path=video, audio_path=audio,
                     title=data.get("title", ""), width=int(data.get("width", 0)),
                     height=int(data.get("height", 0)),
                     acodec=data.get("acodec", ""))


def write_manifest(path: Path, clip: LocalClip) -> None:
    # encoding= and the ValueError arm both matter: this stores the video title,
    # which for a lot of the material this plays is not ASCII, and write_text on
    # Windows defaults to the locale codec. An encoding failure raises
    # UnicodeEncodeError - a ValueError, not an OSError - so the original
    # `except OSError` would not have caught it and a download would have died
    # on a Japanese title.
    try:
        path.write_text(json.dumps({
            "version": MANIFEST_VERSION,
            "video_path": clip.video_path,
            "audio_path": clip.audio_path,
            "title": clip.title,
            "width": clip.width,
            "height": clip.height,
            "acodec": clip.acodec,
        }), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"[chuutube] could not write cache manifest: {exc}", file=sys.stderr)


def prune(cache: Path, max_gb: float = CACHE_MAX_GB,
          min_age_minutes: float = CACHE_MIN_AGE_MIN,
          max_age_hours: float = CACHE_MAX_AGE_H) -> tuple[int, float]:
    """Keep the cache under `max_gb`, oldest first. Returns (files, MB freed).

    There was no policy here at all: clips accumulated until somebody ran
    clear-cache.bat. At ~70MB per 1080p clip that is a folder nobody notices
    growing until a disk is full mid-set.

    Two things are never touched: anything newer than `min_age_minutes`, which
    covers the clip that was just fetched and whatever is on screen; and
    anything the OS refuses to delete, which on Windows is precisely the file
    mpv currently has open. The second is not a fallback for the first - it is
    what makes this safe to run while playing.
    """
    if not cache.is_dir():
        return 0, 0.0

    # Orphaned manifests first, and unconditionally: they are stale pointers,
    # nothing to do with how large the folder is, and skipping the sweep just
    # because the cache happened to be under its limit left them forever.
    for manifest in cache.glob(".manifest-*.json"):
        if read_manifest(manifest) is None:
            try:
                manifest.unlink()
            except OSError:
                pass

    # Cached descriptions go the same way once they are past their window.
    # They are small, but each one holds a PO token and the IP the request came
    # from, so leaving expired ones lying around is worse than untidy.
    for info in cache.glob(".info-*.info.json"):
        try:
            if (time.time() - info.stat().st_mtime) / 60.0 >= INFO_CACHE_MIN:
                info.unlink()
        except OSError:
            pass

    # Age limit next, and like the manifests it does not wait for the folder to
    # be full: a clip from last week is not worth keeping just because there is
    # room for it. This is what bounds the cache when the app is left closed
    # for a while, since nothing else runs then.
    expired, expired_mb = 0, 0.0
    if max_age_hours > 0:
        oldest = time.time() - max_age_hours * 3600
        try:
            candidates = [f for f in cache.iterdir() if f.is_file()]
        except OSError:
            candidates = []
        for f in candidates:
            try:
                stat_result = f.stat()
                if stat_result.st_mtime >= oldest:
                    continue
                size = stat_result.st_size
                f.unlink()
            except OSError:
                # Windows refuses to delete a file mpv has open, which is
                # exactly the protection wanted: never the clip on screen.
                continue
            expired += 1
            expired_mb += size / (1024 ** 2)
        if expired:
            print(f"[chuutube] cache: removed {expired} clip(s) older than "
                  f"{max_age_hours:.0f}h ({expired_mb:.0f} MB)", file=sys.stderr)

    if max_gb <= 0:
        return expired, expired_mb

    limit = max_gb * 1024 ** 3
    cutoff = time.time() - min_age_minutes * 60
    try:
        files = [f for f in cache.iterdir() if f.is_file()]
    except OSError:
        return 0, 0.0

    total = sum(f.stat().st_size for f in files if f.exists())
    if total <= limit:
        return expired, expired_mb

    # Oldest first. mtime rather than atime: Windows updates atime lazily (or
    # not at all), so it is not the signal it looks like.
    removed, freed = 0, 0
    for f in sorted(files, key=lambda x: x.stat().st_mtime if x.exists() else 0):
        if total <= limit:
            break
        try:
            stat_result = f.stat()
        except OSError:
            continue
        if stat_result.st_mtime > cutoff:
            continue                      # too fresh, or in use
        size = stat_result.st_size
        try:
            f.unlink()
        except OSError:
            continue                      # open in mpv - leave it alone
        total -= size
        freed += size
        removed += 1

    if removed:
        print(f"[chuutube] cache over {max_gb:g} GB - removed {removed} old "
              f"file(s), freed {freed / 1024 ** 2:.0f} MB", file=sys.stderr)
    return expired + removed, expired_mb + freed / 1024 ** 2


def clear(cache: Path) -> tuple[int, float]:
    """Delete everything in the cache. Returns (files removed, MB freed).

    Called on exit when CLEAR_CACHE_ON_EXIT is set. Deliberately tolerant: mpv
    is torn down first, but Windows can hold a handle open a moment longer, and
    a file that will not delete now is not worth failing a shutdown over - the
    size cap will take it on the next run.
    """
    if not cache.is_dir():
        return 0, 0.0
    removed, freed = 0, 0
    try:
        entries = list(cache.iterdir())
    except OSError:
        return 0, 0.0
    for f in entries:
        if not f.is_file():
            continue
        try:
            size = f.stat().st_size
            f.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    if removed:
        print(f"[chuutube] cleared the cache on exit: {removed} file(s), "
              f"{freed / 1024 ** 2:.0f} MB", file=sys.stderr)
    return removed, freed / 1024 ** 2


def _explain(tail: list[str], code: int) -> str:
    """Pick the line most likely to tell a human what went wrong."""
    joined = " ".join(tail)
    if "403" in joined and not get_provider().active:
        return ("403 - no PO token provider, so YouTube only serves the "
                "first few MB. Run setup.ps1 to install it.")
    for line in reversed(tail):
        if line.upper().startswith("ERROR:"):
            return re.sub(r"^ERROR:\s*", "", line)[:200]
    for line in reversed(tail):
        if "403" in line or "forbidden" in line.lower():
            return line[:200]
    return f"yt-dlp exited with code {code}"


def download(url: str, max_height: int | None, cache_dir: str | Path,
             progress=None, on_partial=None, on_partial_lost=None,
             cancel=None, ahead: bool = False) -> LocalClip:
    """Fetch video (and audio, if separate) into `cache_dir`."""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    manifest = manifest_path(cache, url, max_height)
    if CACHE_MANIFEST:
        cached = read_manifest(manifest)
        if (cached is not None and FETCH_AUDIO and not cached.has_audio
                and not cached.audio_path):
            # Cached before audio was switched on. Reusing it would play a
            # silent clip and never explain why.
            cached = None
        if cached is not None:
            print(f"[chuutube] cache hit: {cached.video_path}", file=sys.stderr)
            if progress:
                progress(f"cached {cached.height}p" if cached.height else "cached")
            return cached

    ytdlp = ytdlp_command()
    if not ytdlp:
        raise DownloadError(
            "yt-dlp not found or would not run - run setup.ps1 again")

    started = time.monotonic()
    # When mpv was handed the half-written file, if it was. Recorded here rather
    # than in the player because this is the clock the load is measured against.
    seen_at: list[float] = []
    if on_partial is not None:
        _inner_partial = on_partial

        def on_partial(path, _inner=_inner_partial):        # noqa: F811
            taken = _inner(path)
            if taken is not False:
                seen_at.append(time.monotonic())
            return taken

    video_fmt = video_format(max_height)
    # A comma means "fetch both of these as separate files". One invocation,
    # one page extraction, one PO token - instead of two of each.
    want_audio = FETCH_AUDIO
    combined = (f"{video_fmt},bestaudio"
                if want_audio and SINGLE_PASS_AUDIO else video_fmt)

    warm = fresh_info(cache, url)
    if warm is not None:
        # Same video, different quality (or simply loaded again recently). The
        # description lists every format, so this goes straight to the
        # transfer: no client chain, no cookies, no PO token, no challenge.
        if progress:
            progress("using what we already know about this one...")
        print(f"[chuutube] replaying the cached description: {warm.name}",
              file=sys.stderr)
        try:
            entries = _run(ytdlp, url, video_fmt, cache, progress,
                           on_partial=on_partial,
                           on_partial_lost=on_partial_lost, cancel=cancel,
                           info=warm)
            clip = _to_clip(entries)
            if clip is not None:
                _finish(clip, cache, manifest, started, progress,
                        seen_at[0] if seen_at else None, ahead)
                return clip
        except DownloadCancelled:
            raise
        except DownloadError as exc:
            # Signed URLs expire, and not always predictably. Costing a failed
            # attempt is the price of the shortcut; costing the load would not
            # be, so fall through to a full extraction.
            print(f"[chuutube] the cached description did not work "
                  f"({exc}) - extracting again", file=sys.stderr)
            warm.unlink(missing_ok=True)

    if progress:
        progress("fetching video + audio..." if combined != video_fmt
                 else "fetching video...")
    try:
        entries = _run(ytdlp, url, combined, cache, progress,
                       bail_on_format_error=combined != video_fmt,
                       on_partial=on_partial,
                       on_partial_lost=on_partial_lost, cancel=cancel)
    except FormatUnavailable:
        # Nothing here has a separate audio track (or the site does not do
        # multi-format requests). Ask for the video on its own.
        print("[chuutube] no separate audio track - retrying video only",
              file=sys.stderr)
        if progress:
            progress("fetching video...")
        entries = _run(ytdlp, url, video_fmt, cache, progress,
                       on_partial=on_partial, on_partial_lost=on_partial_lost,
                       cancel=cancel)

    clip = _to_clip(entries)
    if clip is None:
        raise DownloadError("yt-dlp finished but produced no file")

    if want_audio and not clip.has_audio and clip.audio_path is None:
        if SINGLE_PASS_AUDIO:
            # The audio was fetched in the same pass; find it by name if the
            # print hook only reported the video.
            clip.audio_path = find_sibling_audio(clip.video_path)
        else:
            if progress:
                progress("fetching audio...")
            try:
                audio = _run(ytdlp, url, "bestaudio/best", cache, progress,
                                 cancel=cancel)
                if audio:
                    clip.audio_path = audio[0].path
            except DownloadError:
                # Silent video beats no video, and audio is muted here anyway.
                clip.audio_path = None

    _finish(clip, cache, manifest, started, progress,
            seen_at[0] if seen_at else None, ahead)
    return clip


def _finish(clip: LocalClip, cache: Path, manifest: Path, started: float,
            progress=None, first_frame: float | None = None,
            ahead: bool = False) -> None:
    """Record the clip and tidy up. Shared by both routes into download()."""
    if CACHE_MANIFEST:
        write_manifest(manifest, clip)
    # Two numbers, because since partial playback they are not the same number.
    # "ready" is when the last byte landed; what the room saw is the first
    # frame, which on a slow link can be most of a minute earlier. Reporting
    # only the first of those made every measurement in this file ambiguous.
    seen = ("" if first_frame is None
            else f" (first frame at {first_frame - started:.1f}s)")
    # "ready" means a load someone is waiting on. A prefetch is nobody waiting,
    # and calling it ready made a 16s prefetch look like a 16s load in the log
    # when the Load that followed it was instant.
    what = "prefetched in" if ahead else "ready in"
    print(f"[chuutube] {what} {time.monotonic() - started:.1f}s{seen}: "
          f"{clip.height}p {clip.video_path}", file=sys.stderr)
    # After, never before: the clip that was just fetched is the newest thing
    # here and the min-age guard protects it.
    prune(cache)
