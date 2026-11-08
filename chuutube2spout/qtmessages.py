# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt's own console messages, with one known-harmless warning made readable.

On Windows, Qt prints `QWindowsWindow::setGeometry: Unable to set geometry
...` when Windows gives a window a slightly different size than Qt asked for.
The tester saw it twice during first-run setup and read it as an error: a
word-wrapped text needed more room than the layout said, and the setup window
came out 51 px taller. Nothing was wrong.

That one message is rewritten into a short, labelled note. Every other Qt
message is passed on unchanged, so real problems still look like problems.
"""

from __future__ import annotations

import re
import sys

# Called with each rewritten note, so a window that is open - the first-run
# setup window - can show it too (K, 0.9.9.37: "in the installation window
# too and not just the log"). Only notes: other Qt messages stay in the log.
_listeners: list = []

_GEOMETRY = re.compile(
    r"QWindowsWindow::setGeometry: Unable to set geometry "
    r"(?P<asked>\d+x\d+)[^\n]*?Resulting geometry: (?P<got>\d+x\d+)")


def rewrite(message: str) -> str:
    """The text to log for one Qt message."""
    found = _GEOMETRY.search(message or "")
    if not found:
        return message
    return (f"[qt] note, harmless: Windows sized a window slightly differently "
            f"than asked ({found['asked']} asked, {found['got']} given). "
            f"Nothing to do.")


def add_listener(fn) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def remove_listener(fn) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def _handler(mode, context, message) -> None:
    try:
        original = str(message)
        text = rewrite(original)
        print(text, file=sys.stderr)
        if text != original:
            for fn in list(_listeners):
                try:
                    fn(text)
                except Exception:
                    pass
    except Exception:
        pass


def install() -> None:
    """Route Qt's messages through rewrite(). Safe to call before QApplication."""
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(_handler)
    except Exception:
        pass        # Qt keeps its default output; nothing depends on this
