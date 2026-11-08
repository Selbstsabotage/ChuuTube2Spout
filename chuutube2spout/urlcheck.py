# SPDX-License-Identifier: GPL-3.0-or-later
"""`check.bat <url>` - work out *why* a URL will not play.

Three causes look identical from the outside and need different fixes:

  1. Your IP is refused. Metadata extraction succeeds, every media fetch 403s.
  2. The player client that produced the URL now needs a PO token - `android_vr`
     went that way in 2026 for everything above 360p.
  3. The request carried the wrong headers. A signed googlevideo URL is bound to
     the client that asked for it; the wrong User-Agent alone earns a 403.

This walks every client, fetches 2 KB of the resulting URL with the headers that
URL was minted for, and prints the status. If *every* client is refused, the
block is on your address rather than your configuration.
"""

from __future__ import annotations

import shutil
import subprocess

from . import APP_NAME
from .config import (AUTO_CLIENT, CLIENT_CHAIN, CODEC_PREFERENCE,
                     COOKIES_FROM_BROWSER)
from .httpu import content_length_from_url
from .resolver import _extract, pick_audio, pick_video, probe

# Offsets to test, in MB. Checking only the start is misleading: these servers
# will hand out an opening chunk and then refuse the rest, so a start-only
# check certifies streams that die a few seconds into playback.
PROBE_POINTS_MB = (0, 5, 12, 25, 40)

def _clients() -> list[str | None]:
    """The chain as the app will actually walk it, each client once.

    "default" is an ordinary entry in CLIENT_CHAIN since 0.9.x, and this used
    to prepend None on top of it - so the report listed `default` twice and
    ended with "Playable via: default, default", which reads as a bug in the
    thing being diagnosed rather than in the diagnosis.
    """
    out: list[str | None] = []
    for name in CLIENT_CHAIN:
        client = None if name.lower() in AUTO_CLIENT else name
        if client not in out:
            out.append(client)
    if None not in out:
        out.append(None)          # the app always has it as a floor
    return out


CLIENTS: list[str | None] = _clients()

# 416 means "there is no such byte", which for a probe past the end of a short
# file is the honest answer rather than a refusal. Printing it as an error made
# a 360p muxed stream - complete, playable, just smaller than the probe points -
# look like it was being blocked.
_PAST_END = "416"


def _js_runtime() -> str:
    exe = shutil.which("deno")
    if not exe:
        return "NOT FOUND - yt-dlp cannot solve YouTube's signature challenges"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=20)
        return f"{(out.stdout or out.stderr).strip().splitlines()[0]}  @ {exe}"
    except Exception as exc:
        return f"found at {exe} but would not run: {exc}"


def _normalise(args: list[str]) -> str:
    """Rebuild a URL that cmd.exe may have split.

    Batch treats `=` as an argument separator, so a bare
    `check.bat https://youtu.be/watch?v=ID` arrives as two arguments. Rejoining
    on `=` reconstructs it; quoting the URL avoids the problem in the first place.
    """
    return "=".join(a for a in args if not a.startswith("-"))


def run(url: str) -> int:
    print(f"{APP_NAME} URL check")
    print("=" * 72)
    print(f"url: {url}")

    try:
        import yt_dlp
        print(f"yt-dlp: {yt_dlp.version.__version__}")
    except ImportError:
        print("yt-dlp is not installed here. Run setup.ps1.")
        return 2

    print(f"JS runtime (deno): {_js_runtime()}")
    print(f"codec preference: {', '.join(CODEC_PREFERENCE)}")
    if COOKIES_FROM_BROWSER:
        print(f"cookies from: {COOKIES_FROM_BROWSER}")
    print("-" * 72)

    extracted, playable, partial, seen_ip = [], [], [], None

    for client in CLIENTS:
        name = client or "default"
        label = f"{name:<14}"
        try:
            info = _extract(url, client, quiet_errors=True)
        except Exception as exc:
            print(f"{label} extract FAILED   {str(exc).replace(chr(10), ' ')[:80]}")
            continue

        formats = info.get("formats") or []
        video = pick_video(formats, None)
        if not video and info.get("url"):
            video = {"url": info["url"], "height": info.get("height"),
                     "vcodec": "?", "http_headers": info.get("http_headers")}
        if not video:
            print(f"{label} extracted, but no playable video formats "
                  f"({len(formats)} formats offered)")
            continue

        extracted.append(name)
        audio = pick_audio(formats)
        height = video.get("height") or "?"
        codec = (video.get("vcodec") or "?").split(".")[0]
        print(f"{label} {len(formats):>3} formats   best: {height}p {codec}"
              f"{'  + separate audio' if audio else '  (muxed)'}")

        headers = dict(video.get("http_headers") or info.get("http_headers") or {})
        if seen_ip is None:
            import re
            m = re.search(r"[?&]ip=([^&]+)", video["url"])
            if m:
                seen_ip = m.group(1)

        total = content_length_from_url(video["url"])
        points = [mb << 20 for mb in PROBE_POINTS_MB
                  if total is None or (mb << 20) < total]
        if total:
            points.append(max(0, total - 4096))     # and the very last bytes

        results, all_ok = [], True
        for offset in points:
            ok, detail = probe(video["url"], headers, offset)
            if not ok and _PAST_END in detail:
                # Not a refusal: the file is simply shorter than this offset,
                # which we only guessed at because Content-Length was unknown.
                results.append(f"{offset >> 20}MB:past end")
                continue
            results.append(f"{offset >> 20}MB:{'ok' if ok else detail.replace('HTTP ', '')}")
            all_ok = all_ok and ok
        size = f"{total / (1 << 20):.1f}MB" if total else "size unknown"
        print(f"{'':<14} {size}  " + "  ".join(results))
        if all_ok:
            playable.append(name)
        elif results and results[0].endswith("ok"):
            partial.append(name)

    print("=" * 72)
    if seen_ip:
        print(f"YouTube sees your traffic as coming from {seen_ip}\n")

    if playable:
        print(f"Playable via: {', '.join(playable)}")
        print(f"\n{APP_NAME} tries clients in this order automatically, so it should")
        print("already work.")
        # "default" means "let yt-dlp choose", which is not a client name -
        # setting PLAYER_CLIENT to it would pass player_client=default to
        # yt-dlp, and there is no such client. It is also already first in the
        # chain, so pinning it would save nothing even if it worked.
        pinnable = [p for p in playable if p != "default"]
        if playable[0] == "default":
            print("It is already trying that one first, so there is nothing to set.")
        elif pinnable:
            print("To skip straight to a known-good one, put this in run.bat:")
            print(f"    set CHUUTUBE2SPOUT_PLAYER_CLIENT={pinnable[0]}")
        return 0

    if partial:
        print(f"Serves the opening chunk then refuses the rest: {', '.join(partial)}")
        print("\nThat is YouTube's PO-token gate, not an IP block - the start of")
        print("the file is free, the rest is not. Signing in is the fix:")
        print("    set CHUUTUBE2SPOUT_COOKIES_FROM_BROWSER=firefox")
        print("  (chrome, edge, brave, vivaldi, opera also accepted; use a browser")
        print("   where you are logged into YouTube, and close it first)")
        return 1

    if extracted:
        print("Every client produced a URL and every media fetch was refused.")
        print("\nMetadata comes from youtube.com and media comes from googlevideo.com.")
        print("Only the second is turning you away, which points at the address")
        print("rather than the configuration.")
        print("\n  -> On a VPN? Turn it off and re-run.")
        print("  -> Otherwise sign in, which lifts most address-level throttling:")
        print("       set CHUUTUBE2SPOUT_COOKIES_FROM_BROWSER=firefox")
        print("     (chrome, edge, brave, vivaldi, opera, safari also accepted)")
        return 1

    print("No client could extract this video at all.")
    print("Check the URL is right and public, then run update-ytdlp.bat.")
    return 1


def main(argv: list[str]) -> int:
    url = _normalise(argv[1:])
    if not url:
        print('usage: check.bat "https://www.youtube.com/watch?v=..."')
        return 2
    return run(url)
