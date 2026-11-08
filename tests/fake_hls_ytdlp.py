#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in yt-dlp that falls back to HLS, so the chain can be tested for real.

The point of these two modes is that the parent has to *stop* the process
partway through, before any bytes move, and then decide whether to come back to
it. That cannot be faked with a return code - the process has to still be alive
and talking when the decision is made - so this is a real subprocess.

Behaviour is chosen by the filename it is invoked as:
    hlsonly_ytdlp     - every client offers only HLS
    hlsthenhttp_ytdlp - the first client offers only HLS, later ones progressive

Every line is flushed. The parent reads line by line and kills us on the
strength of one of them; a block-buffered pipe would mean it never sees it.
"""

import pathlib
import sys
import time

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
for i, arg in enumerate(argv):
    if arg == "--print-to-file" and i + 2 < len(argv):
        print_file = argv[i + 2]
    elif arg == "-o" and i + 1 < len(argv) and not _typed(argv[i + 1]):
        outtmpl = argv[i + 1]

cache = pathlib.Path(outtmpl).parent
cache.mkdir(parents=True, exist_ok=True)

# How many times we have been run for this cache. The chain calls us once per
# client, and "the first client" is the one that only has HLS.
counter = cache / "_invocations"
seen = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(seen))

hls = mode == "hlsonly_ytdlp" or seen == 1


def say(line):
    print(line, flush=True)


say("[youtube] Extracting URL: https://example/watch")

SEP = chr(31)


def finish(name, height, width):
    path = cache / name
    path.write_bytes(b"video")
    pathlib.Path(print_file).write_text(
        SEP.join([str(path), str(height), str(width), "A Clip",
                  "none", "avc1.640028"]), encoding="utf-8")


if hls:
    say('[debug] Invoking hlsnative downloader on "https://manifest.googlevideo.com/x"')
    say("[hlsnative] Downloading m3u8 manifest")
    say("[hlsnative] Total fragments: 46")
    # The parent kills us here, on the strength of the lines above - before the
    # Destination line, which is the whole point: no bytes have moved yet. If it
    # does not, this is the deferred retry and we are meant to go through with it.
    time.sleep(1.5)
    say(f"[download] Destination: {cache / 'clip.f94.mp4'}")
    say("[download] 100% of 1.00MiB")
    finish("clip.f94.mp4", 480, 854)
else:
    say(f"[download] Destination: {cache / 'clip.f137.mp4'}")
    say("[download] 100% of 1.00MiB")
    finish("clip.f137.mp4", 1080, 1920)
