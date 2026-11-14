# SPDX-License-Identifier: GPL-3.0-or-later
"""Quality presets.

The selector logic that used to live here moved to resolver.pick_video, which
walks the real format list instead of writing a yt-dlp format string and hoping.
That is what makes "or the next highest" exact rather than approximate.
"""

from __future__ import annotations

# (label, max height or None for "no ceiling")
PRESETS: list[tuple[str, int | None]] = [
    ("Best available", None),
    ("2160p (4K)", 2160),
    ("1440p", 1440),
    ("1080p", 1080),
    ("720p", 720),
    ("480p", 480),
    ("360p", 360),
]

DEFAULT_HEIGHT = 1080


def label_for(height: int | None) -> str:
    for label, h in PRESETS:
        if h == height:
            return label
    return f"{height}p"
