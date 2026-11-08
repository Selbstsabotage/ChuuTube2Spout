#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for yt-dlp, so the subprocess plumbing can be tested for real.

Behaviour is chosen by the filename it is invoked as:
    good_ytdlp      - succeeds; honours a comma in -f by writing both files
    failing_ytdlp   - prints an ERROR line and exits non-zero
    old_ytdlp       - rejects --js-runtimes the way an older yt-dlp would
    muxed_ytdlp     - one file that already carries audio
    noaudio_ytdlp   - refuses a multi-format request, succeeds on a single one
"""

import pathlib
import sys

argv = sys.argv[1:]
mode = pathlib.Path(sys.argv[0]).stem


# yt-dlp allows several -o, each optionally prefixed with the kind of file it
# applies to ("infojson:PATH"). Only the unprefixed one names the video, and a
# fake that took the last -o it saw would pick up the info-json template and
# write the clip somewhere nobody looks.
_OUT_TYPES = ("infojson:", "thumbnail:", "subtitle:", "description:",
              "pl_thumbnail:", "pl_infojson:", "chapter:", "link:")


def _typed(value):
    return value.startswith(_OUT_TYPES)


print_file = None
outtmpl = None
fmt = ""
for i, arg in enumerate(argv):
    if arg == "--print-to-file" and i + 2 < len(argv):
        print_file = argv[i + 2]
    elif arg == "-o" and i + 1 < len(argv) and not _typed(argv[i + 1]):
        outtmpl = argv[i + 1]
    elif arg == "-f" and i + 1 < len(argv):
        fmt = argv[i + 1]

if "--js-runtimes" in argv and mode == "old_ytdlp":
    print("Usage: yt-dlp [OPTIONS] URL [URL...]")
    print("yt-dlp: error: no such option: --js-runtimes")
    sys.exit(2)

print("[youtube] Extracting URL: https://example/watch")

wants_audio = "," in fmt

if mode == "noaudio_ytdlp" and wants_audio:
    print("ERROR: Requested format is not available. "
          "Use --list-formats for a list of available formats")
    sys.exit(1)

print("[download]  50.0% of   1.00MiB at    1.00MiB/s ETA 00:01")

if mode == "failing_ytdlp":
    print("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    sys.exit(1)

cache = pathlib.Path(outtmpl).parent
cache.mkdir(parents=True, exist_ok=True)
SEP = chr(31)
records = []


def record(name, height, width, acodec, vcodec, payload):
    path = cache / name
    path.write_bytes(payload)
    records.append(SEP.join([str(path), str(height), str(width), "A Clip",
                             acodec, vcodec]))


if mode == "muxed_ytdlp":
    record("clip.f18.mp4", 360, 640, "mp4a.40.2", "avc1.42001E", b"video+audio")
else:
    record("clip.f137.mp4", 1080, 1920, "none", "avc1.640028", b"video")
    if wants_audio:
        record("clip.f251.m4a", 0, 0, "mp4a.40.2", "none", b"audio")

print("[download] 100% of 1.00MiB")
pathlib.Path(print_file).write_text("\n".join(records), encoding="utf-8")
