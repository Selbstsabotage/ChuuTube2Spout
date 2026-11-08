# SPDX-License-Identifier: GPL-3.0-or-later
"""Imported for its side effect: install the stub modules before chuutube2spout loads."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The tests check the built-in defaults. Neither the developer's settings.txt
# nor their environment may change those: before 0.9.9.40 a settings.txt with
# a fixed canvas in the project folder failed eight tests and stopped the
# build. Done here, before chuutube2spout.config is first imported - every
# importlib.reload(config) in the tests reads the environment again.
for _key in [k for k in os.environ
             if k.startswith(("CHUUTUBE2SPOUT_", "YT2" + "SPOUT_"))]:
    del os.environ[_key]
os.environ["CHUUTUBE2SPOUT_SETTINGS_FILE"] = "none"

import stubs  # noqa: E402

stubs.install()
