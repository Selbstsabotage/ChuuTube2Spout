#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Launch ChuuTubePoi. Run this file, or `python -m chuutubepoi`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chuutubepoi.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
