#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in yt-dlp that resumes a finished file past its end.

What the tester's 0.9.9.35 log showed: the clip was already in the cache under
another link, yt-dlp saw the file, asked for the rest and got HTTP 416 - from
every client, because the file, not the client, is the problem. With
--no-continue it starts from zero and succeeds.
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

if "--no-continue" not in argv:
    print("[download] Resuming download at byte 9719397", flush=True)
    print("ERROR: unable to download video data: HTTP Error 416: Requested "
          "range not satisfiable", flush=True)
    sys.exit(1)

print(f"[download] Destination: {cache / 'clip.f137.mp4'}", flush=True)
print("[download] 100% of 1.00MiB", flush=True)
(cache / "clip.f137.mp4").write_bytes(b"video")
(cache / "_args").write_text(" ".join(argv), encoding="utf-8")
pathlib.Path(print_file).write_text(
    chr(31).join([str(cache / "clip.f137.mp4"), "1080", "1920", "A Clip",
                  "none", "avc1.640028"]), encoding="utf-8")
