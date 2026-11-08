#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in yt-dlp that always meets YouTube's bot check.

What K's 0.9.9.30 log showed without cookies: every client got "Sign in to
confirm you're not a bot". Counts its invocations next to the output template
so a test can tell how many clients were tried.
"""

import pathlib
import sys

argv = sys.argv[1:]
outtmpl = None
for i, arg in enumerate(argv):
    if (arg == "-o" and i + 1 < len(argv)
            and not argv[i + 1].startswith("infojson:")):
        outtmpl = argv[i + 1]

cache = pathlib.Path(outtmpl).parent
cache.mkdir(parents=True, exist_ok=True)
counter = cache / "_invocations"
seen = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(seen))

print("[youtube] Extracting URL: https://example/watch", flush=True)
print("WARNING: [youtube] clip: Unable to download webpage: HTTP Error 429: "
      "Too Many Requests", flush=True)
print("ERROR: [youtube] clip: Sign in to confirm you’re not a bot. Use "
      "--cookies-from-browser or --cookies for the authentication.",
      flush=True)
sys.exit(1)
