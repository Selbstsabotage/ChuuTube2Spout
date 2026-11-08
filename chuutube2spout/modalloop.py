# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep rendering while Windows holds the window in its move loop.

Pressing on the title bar starts Windows' move loop at once, before the mouse
moves. In that loop Qt's timers starve, so the render loop stops - and with it
the Spout output, which Resolume shows as a frozen frame (K, 0.9.9.30).
`PreviewSurface.pump()` already covers dragging and resizing, but it runs on
move and resize events, and a press without movement sends none.

What the move loop does deliver is WM_TIMER for a timer set on the window
itself. So: on WM_ENTERSIZEMOVE start one, on each WM_TIMER call the tick, on
WM_EXITSIZEMOVE stop it. Windows rounds the interval to its timer resolution,
usually about 15.6 ms - one frame at 60 fps, enough for video.

Everything here fails quietly: it runs inside Qt's window procedure, where an
exception is worse than a frozen frame.
"""

from __future__ import annotations

import ctypes
import sys
import time

WM_TIMER = 0x0113
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
VK_LBUTTON = 0x01
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232

TIMER_ID = 0x4354          # any number; only compared with our own WM_TIMER
TIMER_MS = 10              # USER_TIMER_MINIMUM; Windows rounds it up

EVENT_TYPE = b"windows_generic_MSG"


class MSG(ctypes.Structure):
    """The start of Win32's MSG: all this needs, the same on every build."""

    _fields_ = [("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t)]


def _user32():
    """A private user32 handle, so setting argtypes affects nobody else."""
    if sys.platform != "win32":
        return None
    try:
        dll = ctypes.WinDLL("user32", use_last_error=True)
        dll.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_uint, ctypes.c_void_p]
        dll.SetTimer.restype = ctypes.c_size_t
        dll.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        dll.KillTimer.restype = ctypes.c_int
        dll.GetAsyncKeyState.argtypes = [ctypes.c_int]
        dll.GetAsyncKeyState.restype = ctypes.c_short
        return dll
    except Exception:
        return None


def _name(event_type) -> bytes:
    """Qt hands the event type over as a QByteArray; tests use bytes."""
    if isinstance(event_type, bytes):
        return event_type
    try:
        return bytes(event_type.data())
    except Exception:
        try:
            return bytes(event_type)
        except Exception:
            return b""


class ModalTicker:
    """Calls `tick` on a Windows timer while the move loop runs."""

    def __init__(self, tick, on_held=None, user32=None) -> None:
        self._tick = tick
        # Told True when the loop starts and False when it ends, so the
        # preview can repaint at once instead of waiting for Qt.
        self._on_held = on_held
        self._user32 = _user32() if user32 is None else user32
        self._hwnd = None
        self._reported = False
        self._since = 0.0
        self._ticks = 0
        # 0.9.9.35: the timer also starts on the press itself, not only when
        # the move loop begins. If Windows waits between the two, that wait
        # was not covered, and the log line now says how long it was.
        self._pressed_at: float | None = None
        self._loop_after: float | None = None

    @property
    def active(self) -> bool:
        return self._hwnd is not None

    def handle(self, event_type, message) -> bool:
        """Look at one native message. True if it was ours and is handled."""
        if self._user32 is None:
            return False
        try:
            if _name(event_type) != EVENT_TYPE:
                return False
            msg = MSG.from_address(int(message))
            kind = msg.message
            if kind == WM_NCLBUTTONDOWN and msg.wParam == HTCAPTION:
                self._start(msg.hwnd)
                self._pressed_at = self._since
            elif kind == WM_ENTERSIZEMOVE:
                if self.active and self._pressed_at is not None:
                    self._loop_after = time.monotonic() - self._pressed_at
                else:
                    self._start(msg.hwnd)
            elif kind == WM_EXITSIZEMOVE:
                self._stop()
            elif kind == WM_TIMER and msg.wParam == TIMER_ID:
                self._run_tick()
                if self._press_ended():
                    self._stop()
                return True
        except Exception as exc:
            self._report(exc)
        return False      # Qt still sees enter/exit; only our timer is eaten

    def _start(self, hwnd) -> None:
        if not hwnd:
            return
        self._stop()
        self._since = time.monotonic()
        self._ticks = 0
        self._pressed_at = None
        self._loop_after = None
        if self._user32.SetTimer(hwnd, TIMER_ID, TIMER_MS, None):
            self._hwnd = hwnd
            self._held(True)
        else:
            print("[chuutube] window held: Windows refused the render timer",
                  file=sys.stderr)

    def _stop(self) -> None:
        if self._hwnd is None:
            return
        try:
            self._user32.KillTimer(self._hwnd, TIMER_ID)
        finally:
            self._hwnd = None
            drawn = self._held(False)
            # One line per hold or drag. Ticks show whether Windows delivered
            # the timer (about 60 a second); frames show whether those ticks
            # drew anything. 0.9.9.33 had the first without the second.
            held = time.monotonic() - self._since
            frames = (f", {drawn} frames drawn" if isinstance(drawn, int)
                      else "")
            if self._pressed_at is None:
                how = ""
            elif self._loop_after is None:
                how = " (title bar pressed, no move loop)"
            else:
                how = (f" (title bar pressed, move loop after "
                       f"{self._loop_after * 1000:.0f}ms)")
            print(f"[chuutube] window held {held:.1f}s{how}: {self._ticks} "
                  f"render ticks from the Windows timer{frames}",
                  file=sys.stderr)
            self._pressed_at = None
            self._loop_after = None

    def _press_ended(self) -> bool:
        """A title-bar press that never became a move loop, now released."""
        if self._pressed_at is None or self._loop_after is not None:
            return False
        state = getattr(self._user32, "GetAsyncKeyState", None)
        if state is None:
            return False
        return not (int(state(VK_LBUTTON)) & 0x8000)

    def _held(self, value: bool):
        if self._on_held is None:
            return None
        try:
            return self._on_held(value)
        except Exception as exc:
            self._report(exc)
            return None

    def _run_tick(self) -> None:
        self._ticks += 1
        try:
            self._tick()
        except Exception as exc:
            self._report(exc)

    def _report(self, exc: Exception) -> None:
        # Once: a message per timer tick would be sixty lines a second.
        if not self._reported:
            self._reported = True
            print(f"[chuutube] could not keep rendering while the window is "
                  f"held ({exc.__class__.__name__}: {exc})", file=sys.stderr)
