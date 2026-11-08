#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for yt-dlp that reports a Destination and then keeps going.

For the partial-playback hand-over: the downloader offers the half-written file
on progress lines after the Destination, so this prints several of them.
"""

import pathlib
import sys
import time

argv = sys.argv[1:]
print_file = outtmpl = None
for i, arg in enumerate(argv):
    if arg == "--print-to-file" and i + 2 < len(argv):
        print_file = argv[i + 2]
    elif (arg == "-o" and i + 1 < len(argv)
          and not argv[i + 1].startswith("infojson:")):
        outtmpl = argv[i + 1]

cache = pathlib.Path(outtmpl).parent
cache.mkdir(parents=True, exist_ok=True)
clip = cache / "clip.f137.mp4"
print("[youtube] Extracting URL: https://example/watch", flush=True)
print(f"[download] Destination: {clip}", flush=True)
clip.write_bytes(b"x" * 4096)
for pct in (10, 20, 30, 40, 50, 60, 70, 80, 90):
    print(f"[download]  {pct}.0% of   1.00MiB at    1.00MiB/s ETA 00:01",
          flush=True)
    time.sleep(0.01)
print("[download] 100% of 1.00MiB", flush=True)
SEP = chr(31)
pathlib.Path(print_file).write_text(
    SEP.join([str(clip), "1080", "1920", "A Clip", "none", "avc1.640028"]),
    encoding="utf-8")
