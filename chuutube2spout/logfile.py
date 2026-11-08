# SPDX-License-Identifier: GPL-3.0-or-later
"""Tee the console to a file, so a bug report does not depend on a console.

Everything this app knows how to say about itself it says on stderr. That was
fine while the only way to run it was run.bat, which leaves a console window
open behind the app - but the built exe is double-clicked, and a tester who
hits a problem has nothing to send. "No logs for this" is not a rare answer; it
is the default one.

So stdout and stderr are wrapped on the way in. They still go where they went
before (the console, when there is one), and they also go to a timestamped file
next to the app. Nothing that already prints has to change.

Three details that are not obvious:

* **Lines, not writes.** print() writes the text and the newline separately,
  and some callers write in fragments. Redaction has to see a whole line or it
  can miss a credential split across two writes, so the file side buffers until
  a newline.
* **Redacted.** redact.scrub already runs on yt-dlp's output before it is
  printed. It runs again here because the file is the thing that gets attached
  to an issue, and this is the last point where anything can be caught -
  including output from code that forgot to scrub.
* **Flushed on a timer, and immediately for anything alarming.** A log
  missing its last twenty lines is missing the ones that mattered - but a
  synchronous disk flush per line is GUI-thread work when the line came from
  the GUI thread, and mpv and yt-dlp are both chatty at their default levels.
  So a background thread flushes every FLUSH_EVERY_S whether or not anything
  else is written (see _flusher for why "whether or not" is the whole point),
  and a line that mentions an error is flushed at once.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import threading
import time as _time
from pathlib import Path

from .redact import scrub

LOG_PREFIX = "chuutube-"
LOG_SUFFIX = ".log"

# How long a run of ordinary lines may sit in the file object's buffer.
FLUSH_EVERY_S = 0.25

# Lines worth paying a disk sync for the instant they are written, because the
# next thing to happen may be the process disappearing.
URGENT = ("error", "fatal", "traceback", "exception", "failed", "cannot")

_started: Path | None = None
_handle = None

# Both Tees - stdout and stderr - write into the ONE file object, from the GUI
# thread, the mpv event threads, the yt-dlp reader threads and the flusher at
# once. The unfinished-line buffers are per thread (see Tee.__init__) and need
# no lock; this one guards the file object and the dirty flag.
_file_lock = threading.Lock()


class Tee:
    """Writes to the original stream and to the log file.

    Deliberately tolerant: a frozen build can hand us `None` for a stream, a
    closed console makes writes raise, and neither is a reason to take the app
    down. If one side fails the other still gets the line.
    """

    def __init__(self, stream, handle) -> None:
        self.stream = stream
        self._handle = handle
        # One unfinished line PER THREAD. print() writes the text and the
        # newline as two calls, and a shared buffer let another thread's text
        # land between them: two threads' fragments merged into one line, and
        # a URL could be cut in half by somebody else's newline - with the
        # half carrying the token reaching the file unscrubbed. Per thread, a
        # line only ever ends with its own newline.
        self._local = threading.local()

    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        if self.stream is not None:
            try:
                self.stream.write(text)
            except Exception:
                pass
        self._to_file(text)
        return len(text)

    def _to_file(self, text: str) -> None:
        if self._handle is None:
            return
        buffer = getattr(self._local, "buffer", "") + text
        if "\n" not in buffer:
            # Hold an unterminated fragment: scrubbing half a URL can leave the
            # half with the credential in it looking clean.
            if len(buffer) < 8192:
                self._local.buffer = buffer
                return
            buffer += "\n"                # pathological line, cut it loose
        head, _, self._local.buffer = buffer.rpartition("\n")
        line = scrub(head + "\n")
        with _file_lock:
            try:
                self._handle.write(line)
                _mark_dirty()
                lowered = head.lower()
                if any(word in lowered for word in URGENT):
                    self._handle.flush()
                    _mark_clean()
            except Exception:
                self._handle = None       # disk full, or the file went away

    def flush(self) -> None:
        if self.stream is not None:
            try:
                self.stream.flush()
            except Exception:
                pass
        if self._handle is None:
            return
        pending = getattr(self._local, "buffer", "")
        self._local.buffer = ""
        with _file_lock:
            try:
                if pending:
                    self._handle.write(scrub(pending))
                # An explicit flush() is always honoured - it is what the
                # excepthook and the shutdown path use to make sure the last
                # lines are on disk.
                self._handle.flush()
                _mark_clean()
            except Exception:
                self._handle = None

    def isatty(self) -> bool:
        try:
            return bool(self.stream is not None and self.stream.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        # Subprocesses ask for this when they inherit our handles. Answer with
        # the real console's, never the log file's - a child writing straight
        # into the log would interleave with our buffered lines.
        if self.stream is None:
            raise OSError("no console attached")
        return self.stream.fileno()

    def __getattr__(self, name):
        # encoding, errors, buffer, writable... whatever asks, the real stream
        # is the honest answer.
        return getattr(self.stream, name)


def log_dir(root: Path | None = None) -> Path:
    from .config import LOG_DIR
    if LOG_DIR:
        return Path(LOG_DIR)
    if root is None:
        from .bootstrap import project_root
        root = project_root()
    return Path(root) / "logs"


def prune(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` logs. Returns what was removed.

    By name rather than by mtime: the name carries the start time, and a log
    that was still being written to when the app was killed has an mtime that
    says nothing useful about which session it belongs to.
    """
    if keep <= 0:
        return []
    try:
        logs = sorted(p for p in directory.iterdir()
                      if p.is_file() and p.name.startswith(LOG_PREFIX)
                      and p.name.endswith(LOG_SUFFIX))
    except OSError:
        return []
    removed = []
    for old in logs[:max(0, len(logs) - keep)]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass              # in use by another instance, or read-only
    return removed


def _header() -> str:
    from . import APP_NAME, __version__
    from .config import MODE, SETTINGS_FILE
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join((
        f"{APP_NAME} {__version__}",
        f"started  {now}",
        f"python   {sys.version.split()[0]} on {sys.platform}",
        f"frozen   {bool(getattr(sys, 'frozen', False))}",
        f"mode     {MODE}",
        f"settings {SETTINGS_FILE or 'none - defaults'}",
        "-" * 68,
        "",
    ))


def start() -> Path | None:
    """Begin logging. Returns the file being written, or None if disabled.

    Safe to call twice - the second call is a no-op and returns the same path.
    """
    global _started, _handle
    if _started is not None:
        return _started

    from .config import LOG_KEEP, LOG_TO_FILE
    if not LOG_TO_FILE:
        return None

    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[chuutube] no log file: cannot create {directory} ({exc})",
              file=sys.stderr)
        return None

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    # The pid disambiguates two instances started in the same second, which is
    # exactly what happens when someone double-clicks twice.
    path = directory / f"{LOG_PREFIX}{stamp}-{os.getpid()}{LOG_SUFFIX}"
    try:
        # No newline= argument on purpose: the default translates "\n" to the
        # platform ending, so the file opens readably in Notepad.
        _handle = open(path, "w", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[chuutube] no log file: cannot write {path} ({exc})",
              file=sys.stderr)
        return None

    _handle.write(_header())
    _handle.flush()

    sys.stdout = Tee(sys.stdout, _handle)
    sys.stderr = Tee(sys.stderr, _handle)
    _started = path

    # Prune AFTER opening ours, so this run's file is one of the ones kept and
    # a keep of 1 does not delete the log being written.
    prune(directory, LOG_KEEP)

    _install_excepthook()
    _enable_faulthandler(_handle)
    # Ordinary lines sit in the file object's buffer for at most FLUSH_EVERY_S
    # before the flusher pushes them out; a clean exit pushes the rest.
    threading.Thread(target=_flusher, daemon=True, name="log-flush").start()
    import atexit
    atexit.register(_flush_all)
    print(f"[chuutube] logging to {path}", file=sys.stderr)
    return path


_dirty = False


def _mark_dirty() -> None:
    global _dirty
    _dirty = True


def _mark_clean() -> None:
    global _dirty
    _dirty = False


def _flusher() -> None:
    """Push buffered lines to disk on a timer, whether or not more arrive.

    0.9.9.9 flushed "when a line is written and 250ms have passed" - which only
    ever runs when ANOTHER line is written. A quiet stretch left the tail of the
    log in memory indefinitely, and a hard crash lost it. That is exactly what
    happened on a tester's machine: slot B's decoder and video-output lines
    were written, nothing followed them, and the crash that came seconds later
    took them with it - so the one log that could have said why B was black
    could not. The claim that the exposure was "a fraction of a second" was
    wrong, and this is the fix for it.

    Complete lines only. An unterminated fragment stays in the Tee until its
    newline arrives, so redaction still always sees whole lines.
    """
    while True:
        _time.sleep(FLUSH_EVERY_S)
        if not _flush_once():
            return


def _flush_once(handle=None) -> bool:
    """One pass of the flusher. False once the file is unusable."""
    handle = handle if handle is not None else _handle
    if handle is None:
        return True
    if not _dirty:
        return True
    with _file_lock:
        try:
            handle.flush()
            _mark_clean()
        except Exception:
            return False
    return True


def _flush_all() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def _install_excepthook() -> None:
    """A crash should land in the log, not only in a console nobody kept."""
    previous = sys.excepthook

    def hook(kind, value, tb):
        try:
            import traceback
            text = "".join(traceback.format_exception(kind, value, tb))
            print("\n[chuutube] unhandled exception:\n" + text,
                  file=sys.stderr, end="")
            sys.stderr.flush()
        except Exception:
            pass
        previous(kind, value, tb)

    sys.excepthook = hook


def _enable_faulthandler(handle) -> None:
    """Catch the crashes Python cannot: a segfault in a GPU or codec driver.

    This app drives libmpv, an OpenGL context and a Spout texture share. When
    one of those dies it dies natively, and Python's own machinery never runs -
    the process simply vanishes. faulthandler writes the Python stack from an
    exception handler, which is the only thing that still works at that point.

    **all_threads=False, and it has to stay False.** This was True in 0.9.9.6
    to 0.9.9.11, and on a tester's machine it crashed the app. On Windows
    faulthandler is a *vectored* handler: it runs for every first-chance SEH
    exception with the high bit set, including ones that are about to be
    caught. LuaJIT - inside libmpv, running mpv's built-in scripts - raises and
    catches its own errors that way (0xE24C4A00 | the Lua error code; 0x02 is
    LUA_ERRRUN), so every harmless Lua error was logged as "Windows fatal
    exception" and triggered a dump of EVERY Python thread, walked without the
    GIL, while those threads kept running because nothing was actually wrong.
    Walking a frame that another thread is popping reads freed memory: the log
    showed a dump reach the mpv event thread mid-callback, print "File ???",
    and die of an access violation - inside faulthandler - with the reentrancy
    guard left set so every later dump printed a header and nothing else.

    With False it only ever reads the stack of the thread that raised, which
    is by definition not running anything else. A genuine crash on the GUI
    thread still gets the GUI thread's stack, which is the one that matters.
    The built-in Lua scripts are also switched off now (player.py), so the
    false alarms should stop at the source as well.
    """
    try:
        import faulthandler
        faulthandler.enable(file=handle, all_threads=False)
    except Exception:
        pass


def path() -> Path | None:
    """The log being written this run, or None."""
    return _started
