# SPDX-License-Identifier: GPL-3.0-or-later
"""First-run install of the native runtime, from inside the app.

The release deliberately ships no libmpv, no deno and no PO token generator -
see THIRD-PARTY-NOTICES.md. Something still has to fetch them, and asking a VJ
to find a .bat before the thing they downloaded will open is a poor first
impression. So the app offers to do it: same download, same verification, same
script, just reached by pressing a button instead of by reading a README.

It shells out to the bundled setup.ps1 rather than reimplementing the fetch in
Python. That script already knows the projects, the checksums, the trusted
hosts, and the archive layouts - and it is the version that has actually been
tested. A second implementation of a security check is how one of the two rots.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .bootstrap import project_root


def runtime_command(root: Path | None = None) -> list[str] | None:
    """How to run the bundled installer, or None if it is not there.

    -NoProfile because a user profile that prints a banner, or changes the
    working directory, is not something this should inherit.
    """
    root = project_root() if root is None else Path(root)
    script = root / "setup.ps1"
    if not script.is_file():
        return None
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-RuntimeOnly"]


def should_offer() -> bool:
    """Only in a release build, and only if the installer travelled with it.

    From source, setup.ps1 without -RuntimeOnly is the right command and it
    does more than this would - offering the cut-down version there would be
    steering someone towards the wrong one.
    """
    return bool(getattr(sys, "frozen", False)) and runtime_command() is not None


def _forget_failed_imports() -> None:
    """Drop the half-imported modules so the retry is a real one.

    Python discards a module whose import raised, but the packages *around* it
    can be left in sys.modules pointing at nothing useful. Clearing ours makes
    the second `from .ui import MainWindow` do the work again rather than hand
    back the wreckage of the first.
    """
    for name in list(sys.modules):
        if name == "mpv" or name.startswith("chuutube2spout.ui") \
                or name.startswith("chuutube2spout.player") \
                or name.startswith("chuutube2spout.surface"):
            sys.modules.pop(name, None)


class _Installer:
    """Runs the script and reports its output line by line."""

    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.returncode: int | None = None

    def run(self, on_line) -> int:
        def report(text: str) -> None:
            # Into the app's log file as well as the window: stderr is teed
            # there. The window closes itself on success, and until 0.9.9.18
            # nothing else kept what the install said - so 0.9.9.17's first
            # run lost the one line saying why libmpv was missing.
            print(f"[setup] {text}", file=sys.stderr)
            on_line(text)

        try:
            proc = subprocess.Popen(
                self.command, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=str(project_root()),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:                      # no PowerShell, blocked
            report(f"Could not start the installer: {exc}")
            self.returncode = -1
            return -1
        for line in proc.stdout:
            report(line.rstrip())
        proc.wait()
        try:
            proc.stdout.close()
        except Exception:
            pass
        self.returncode = proc.returncode
        print(f"[setup] installer exited with code {proc.returncode}",
              file=sys.stderr)
        return proc.returncode


def offer_runtime_install(exc: BaseException) -> bool:
    """Show the first-run window. True if the runtime is now installed.

    Returning False covers both "they chose not to" and "it did not work" -
    the caller falls back to the written instructions either way, which is the
    honest outcome when a download this app does not control has failed.
    """
    if not should_offer():
        return False

    from PySide6.QtCore import QObject, Qt, QThread, Signal
    from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                                   QProgressBar, QPushButton, QVBoxLayout)

    from . import APP_NAME
    from .theme import STYLESHEET

    command = runtime_command()

    class Worker(QThread):
        line = Signal(str)
        finished_with = Signal(int)

        def run(self) -> None:
            installer = _Installer(command)
            code = installer.run(self.line.emit)
            self.finished_with.emit(code)

    dialog = QDialog()
    dialog.setWindowTitle(f"{APP_NAME} - one-time setup")
    dialog.setStyleSheet(STYLESHEET)
    dialog.setMinimumWidth(620)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)

    headline = QLabel("The video engine has not been downloaded yet.")
    headline.setStyleSheet("font-size: 15px; font-weight: 600;")
    layout.addWidget(headline)

    why = QLabel(
        "ChuuTube2Spout needs libmpv, deno and a PO token generator. They are "
        "not included in the download because they are other people's work "
        "under their own licences - so your copy comes from the projects that "
        "publish them, checked against the hashes in checksums.txt.\n\n"
        "This needs the internet once, and takes a few minutes. After it, "
        "the app only goes online to fetch clips.")
    why.setWordWrap(True)
    why.setObjectName("dim")
    layout.addWidget(why)

    log = QPlainTextEdit()
    log.setReadOnly(True)
    log.setVisible(False)
    log.setMinimumHeight(220)
    layout.addWidget(log)

    bar = QProgressBar()
    bar.setRange(0, 0)           # no honest percentage to report
    bar.setVisible(False)
    layout.addWidget(bar)

    buttons = QHBoxLayout()
    buttons.addStretch(1)
    quit_btn = QPushButton("Not now")
    install_btn = QPushButton("Download and install")
    install_btn.setProperty("role", "paste")     # the one to press
    buttons.addWidget(quit_btn)
    buttons.addWidget(install_btn)
    layout.addLayout(buttons)

    state = {"ok": False}

    def append(text: str) -> None:
        log.appendPlainText(text)

    def done(code: int) -> None:
        bar.setVisible(False)
        if code == 0:
            state["ok"] = True
            headline.setText("Done - starting the app.")
            dialog.accept()
            return
        headline.setText("That did not work.")
        why.setText(
            "The log below says why. You can close this, then run "
            "setup-runtime.bat in the same folder to try again - it does "
            "exactly the same thing from a command window, where the error "
            "will be easier to read.")
        install_btn.setEnabled(True)
        install_btn.setText("Try again")
        quit_btn.setText("Close")

    def start() -> None:
        install_btn.setEnabled(False)
        log.setVisible(True)
        bar.setVisible(True)
        headline.setText("Downloading...")
        dialog.worker = Worker()                  # kept alive on the dialog
        dialog.worker.line.connect(append, Qt.QueuedConnection)
        dialog.worker.finished_with.connect(done, Qt.QueuedConnection)
        dialog.worker.start()

    install_btn.clicked.connect(start)
    quit_btn.clicked.connect(dialog.reject)

    # Qt's harmless window-size warning (see qtmessages.py) appears while this
    # window opens and grows. The tester read it as a setup error, so it is
    # shown here as well, as a labelled note. Through a queued signal: Qt can
    # call its message handler from any thread.
    class _Notes(QObject):
        text = Signal(str)

    notes = _Notes()
    notes.text.connect(append, Qt.QueuedConnection)
    from . import qtmessages
    # One object, kept: the same callable has to be removed again.
    forward = notes.text.emit
    qtmessages.add_listener(forward)
    try:
        dialog.exec()
    finally:
        qtmessages.remove_listener(forward)

    if state["ok"]:
        from .bootstrap import prepare_native_paths
        prepare_native_paths()
        _forget_failed_imports()
    return state["ok"]
