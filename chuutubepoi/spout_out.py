# SPDX-License-Identifier: GPL-3.0-or-later
"""Spout senders.

Spout shares a GPU texture between processes on Windows via DirectX 11. Sending
is a GPU-local copy - a few hundred MB/s at 1080p60, on a bus that does hundreds
of GB/s - which is why publishing A and B simultaneously costs nothing you can
measure.

On non-Windows (or if SpoutGL is missing) every call becomes a no-op so the rest
of the app still runs, which is what makes the thing testable off-Windows.

On macOS the app sends Syphon instead (syphon_out.py, since 1.1).
create_output() at the bottom picks one; both have the same interface.
"""

from __future__ import annotations

import sys
from typing import Optional

from .config import SENDER_A, SENDER_B

try:
    import SpoutGL  # type: ignore
    from OpenGL import GL

    SPOUT_AVAILABLE = True
    SPOUT_ERROR = ""
except Exception as exc:  # pragma: no cover - platform dependent
    SpoutGL = None  # type: ignore
    SPOUT_AVAILABLE = False
    SPOUT_ERROR = (
        "SpoutGL is not available - Spout output is disabled. "
        + ("Spout is Windows-only." if sys.platform != "win32" else f"({exc})")
    )


class Sender:
    """One named Spout sender, created lazily on first frame."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._sender = None
        self._enabled = True
        self.frames = 0

    @property
    def active(self) -> bool:
        return self._sender is not None

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self.release()

    def send(self, texture_id: int, width: int, height: int, invert: bool) -> bool:
        if not (SPOUT_AVAILABLE and self._enabled) or texture_id <= 0:
            return False
        if width <= 0 or height <= 0:
            return False
        if self._sender is None:
            self._sender = SpoutGL.SpoutSender()
            self._sender.setSenderName(self.name)
        ok = self._sender.sendTexture(texture_id, GL.GL_TEXTURE_2D,
                                      width, height, invert, 0)
        if ok:
            self.frames += 1
            # Lets receivers that opt into frame sync avoid tearing.
            try:
                self._sender.setFrameSync(self.name)
            except Exception:
                pass
        return bool(ok)

    def release(self) -> None:
        if self._sender is not None:
            try:
                self._sender.releaseSender()
            except Exception:
                pass
            self._sender = None


class SpoutOutput:
    """The app's two senders, one per player.

    There was a third, optional "program" sender that followed a LIVE button
    in the window; removed in 0.9.9.20. Switching between A and B happens in
    Resolume, which is where a VJ already does it.
    """

    kind = "Spout"

    def __init__(self, name_a: str = SENDER_A, name_b: str = SENDER_B) -> None:
        self.players = [Sender(name_a), Sender(name_b)]

    @property
    def available(self) -> bool:
        return SPOUT_AVAILABLE

    @property
    def unavailable_reason(self) -> str:
        return SPOUT_ERROR

    def publish(self, targets, invert: bool, changed=None) -> None:
        """Send the targets that changed. `changed` is a per-target bool list.

        Sending is a GPU copy taken on the GUI thread, so doing it for frames
        that have not moved is pure cost.
        """
        for index, (sender, target) in enumerate(zip(self.players, targets)):
            if changed is not None and not changed[index]:
                continue
            if target is not None and target.valid:
                sender.send(target.tex, target.w, target.h, invert)

    def names(self) -> list[str]:
        return [s.name for s in self.players]

    def shutdown(self) -> None:
        for s in self.players:
            s.release()


def output_kind(platform: str | None = None) -> str:
    """"Syphon" on macOS, "Spout" everywhere else."""
    return "Syphon" if (platform or sys.platform) == "darwin" else "Spout"


def create_output(platform: str | None = None):
    """The sender pair for this platform: Syphon on macOS, Spout elsewhere.

    Imported here, not at the top: syphon-python pulls in PyObjC, which
    exists only on a Mac.
    """
    if output_kind(platform) == "Syphon":
        from .syphon_out import SyphonOutput
        return SyphonOutput()
    return SpoutOutput()
