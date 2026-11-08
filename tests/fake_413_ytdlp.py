#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in yt-dlp that 413s whenever it is given cookies.

That is what YouTube does to a request whose headers are too big, and the jar
is what makes them big - so it fails for every client identically until the
cookies stop being sent. Modelled on --cookies being present in argv, because
that is exactly the condition the chain has to react to.
"""

import pathlib
import sys

argv = sys.argv[1:]


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

counter = cache / "_invocations"
seen = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(seen))

print("[youtube] Extracting URL: https://example/watch", flush=True)

if "--cookies" in argv:
    print("WARNING: [youtube] HTTP Error 413: Request Entity Too Large. "
          "Retrying (1/3)...", flush=True)
    print("ERROR: [youtube] clip: Unable to download API page: "
          "HTTP Error 413: Request Entity Too Large", flush=True)
    sys.exit(1)

print(f"[download] Destination: {cache / 'clip.f137.mp4'}", flush=True)
print("[download] 100% of 1.00MiB", flush=True)
(cache / "clip.f137.mp4").write_bytes(b"video")
pathlib.Path(print_file).write_text(
    chr(31).join([str(cache / "clip.f137.mp4"), "1080", "1920", "A Clip",
                  "none", "avc1.640028"]), encoding="utf-8")
