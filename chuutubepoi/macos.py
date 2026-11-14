# SPDX-License-Identifier: GPL-3.0-or-later
"""macOS-only glue. Everything here does nothing on other platforms.

**App Nap.** macOS slows down the timers of an app that is in the background,
and during a show the app in front is Resolume. The render tick - which feeds
Syphon - would then run late or in bursts, and Resolume would show it as a
stutter. An app opts out by holding an "activity" for as long as the work
lasts: NSProcessInfo.beginActivityWithOptions:reason:.

The options: UserInitiated (the user started this and is waiting on it, which
also keeps the computer from idle sleep while the app runs) and
LatencyCritical (timers must fire on time, not be grouped to save power).
Display sleep is left to the system settings.

PyObjC (pyobjc-framework-Cocoa, from requirements.txt) makes the call.
syphon-python is built on PyObjC too.
"""

from __future__ import annotations

import sys

# From NSProcessInfo.h. Written out because the PyObjC constants are only
# there on a Mac, and the values are fixed by Apple's headers.
NS_ACTIVITY_IDLE_SYSTEM_SLEEP_DISABLED = 1 << 20
NS_ACTIVITY_USER_INITIATED = 0x00FFFFFF | NS_ACTIVITY_IDLE_SYSTEM_SLEEP_DISABLED
NS_ACTIVITY_LATENCY_CRITICAL = 0xFF00000000
ACTIVITY_OPTIONS = NS_ACTIVITY_USER_INITIATED | NS_ACTIVITY_LATENCY_CRITICAL
REASON = "Sending video to Syphon"


def _process_info():
    from Foundation import NSProcessInfo  # type: ignore
    return NSProcessInfo.processInfo()


def keep_awake(platform: str | None = None):
    """Opt out of App Nap for this process. Returns the activity, or None.

    The caller keeps the returned object for the whole session: the activity
    ends when it is released. Never raises - a failure costs smoothness, not
    the window - and says in the log what happened.
    """
    if (platform or sys.platform) != "darwin":
        return None
    try:
        activity = _process_info().beginActivityWithOptions_reason_(
            ACTIVITY_OPTIONS, REASON)
    except Exception as exc:
        print(f"[chuutube] App Nap: could not opt out ({exc}) - Syphon may "
              "stutter while another app is in front", file=sys.stderr)
        return None
    print("[chuutube] App Nap: off for this session (timers keep running "
          "with another app in front)", file=sys.stderr)
    return activity


def keep_awake_available(platform: str | None = None) -> tuple[bool, str]:
    """Can keep_awake() work here? For --doctor. Starts nothing."""
    if (platform or sys.platform) != "darwin":
        return False, "not applicable - macOS only"
    try:
        info = _process_info()
    except Exception as exc:
        return False, (f"PyObjC (Foundation) is missing: {exc} - "
                       "Syphon may stutter with another app in front. "
                       "Run setup.sh again.")
    if not hasattr(info, "beginActivityWithOptions_reason_"):
        return False, "this macOS has no NSProcessInfo activities"
    return True, "available - the app holds it while it runs"
