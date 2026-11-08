#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Launch ChuuTube2Spout. Run this file, or `python -m chuutube2spout`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chuutube2spout.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
