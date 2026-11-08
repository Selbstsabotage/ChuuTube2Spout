# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate native dependencies BEFORE anything imports the `mpv` module.

python-mpv resolves libmpv through ctypes.util.find_library(), which on Windows
only searches %PATH%. So we prepend our own lib directories to PATH (and register
them as DLL directories for Python 3.8+ dependent-DLL resolution) here, and this
module must be imported before `import mpv` anywhere in the app.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Directories that may hold mpv-2.dll / libmpv-2.dll, relative to the project root.
_LIB_SUBDIRS = ("", "lib", "bin")


def project_root() -> Path:
    """The directory the app was installed into (works frozen and from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def prepare_native_paths() -> list[Path]:
    """Make bundled native libraries discoverable. Returns the dirs we added."""
    added: list[Path] = []
    root = project_root()
    for sub in _LIB_SUBDIRS:
        d = (root / sub) if sub else root
        if not d.is_dir():
            continue
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass
        added.append(d)
    return added


def find_ytdlp() -> str | None:
    """Path to a yt-dlp executable, or None.

    mpv's ytdl_hook looks for `yt-dlp` on PATH by itself, but being explicit
    means a venv-installed copy is found even when the app is launched oddly.
    """
    root = project_root()
    candidates = [
        root / "yt-dlp.exe",
        root / "bin" / "yt-dlp.exe",
        root / ".venv" / "Scripts" / "yt-dlp.exe",
        root / ".venv" / "bin" / "yt-dlp",
    ]
    # Alongside the running interpreter (i.e. pip-installed into this env).
    interp_dir = Path(sys.executable).resolve().parent
    candidates += [interp_dir / "yt-dlp.exe", interp_dir / "yt-dlp"]
    for c in candidates:
        if c.is_file():
            return str(c)
    found = shutil.which("yt-dlp")
    return found


# When frozen, the app re-enters itself to be yt-dlp. See ytdlp_command().
YTDLP_FLAG = "--yt-dlp"

_YTDLP_COMMAND: list[str] | None = None


def _runs(command: list[str]) -> bool:
    try:
        done = subprocess.run(command + ["--version"], capture_output=True,
                              timeout=60,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
        return done.returncode == 0
    except Exception:
        return False


def ytdlp_command(refresh: bool = False) -> list[str] | None:
    """How to invoke yt-dlp: argv prefix, or None if it is not installed.

    Not just a path, because pip's Windows console scripts are little launcher
    executables with the absolute path to python.exe baked into them. Rename or
    move the folder and `.venv\\Scripts\\yt-dlp.exe` stops working with "Fatal
    error in launcher: Unable to create process" - which for an app whose whole
    pitch is "copy the folder to the gig laptop" is not a corner case.

    So the shim is tried, and if it will not run we fall back to invoking the
    module through the interpreter that is already running us. That path has no
    baked-in absolute anything and survives being moved.
    """
    global _YTDLP_COMMAND
    if _YTDLP_COMMAND is not None and not refresh:
        return _YTDLP_COMMAND

    if getattr(sys, "frozen", False):
        # In a PyInstaller build there is no .venv and no yt-dlp.exe, and
        # sys.executable is OUR exe - so [sys.executable, "-m", "yt_dlp"] would
        # silently launch a second copy of the GUI instead of a downloader.
        # Instead the exe re-enters itself with a flag that app.main() answers
        # by handing straight over to yt-dlp's own entry point. One binary, and
        # yt-dlp still runs as a real subprocess with its own stdout to forward.
        frozen = [sys.executable, YTDLP_FLAG]
        if _runs(frozen):
            _YTDLP_COMMAND = frozen
            return _YTDLP_COMMAND
        print("[chuutube] this build cannot run yt-dlp - it was probably "
              "packaged without it (--collect-all yt_dlp)", file=sys.stderr)
        _YTDLP_COMMAND = None
        return None

    exe = find_ytdlp()
    if exe and _runs([exe]):
        _YTDLP_COMMAND = [exe]
        return _YTDLP_COMMAND

    module = [sys.executable, "-m", "yt_dlp"]
    if _runs(module):
        if exe:
            print("[chuutube] yt-dlp.exe would not start (the folder was "
                  "probably renamed) - using 'python -m yt_dlp' instead",
                  file=sys.stderr)
        _YTDLP_COMMAND = module
        return _YTDLP_COMMAND

    _YTDLP_COMMAND = None
    return None


def mpv_import_error_help(exc: BaseException) -> str:
    """A message that actually tells you what to do, instead of a ctypes traceback."""
    root = project_root()
    if sys.platform == "win32":
        if getattr(sys, "frozen", False):
            # The expected first run of a release: the native pieces are not in
            # the zip on purpose, so this is not a fault, it is step one.
            return (
                "The video engine could not be downloaded or loaded.\n\n"
                "Run this in the folder you unzipped, where the error will be\n"
                "easier to read than it is here:\n"
                "    setup-runtime.bat\n\n"
                "It fetches libmpv, deno and the PO token generator from the "
                "projects\nthat publish them, checked against the hashes in "
                "checksums.txt. It needs\nthe internet once; after that the app "
                "only goes online to fetch clips.\n\n"
                "They are not included in the download because shipping them "
                "would mean\nredistributing other people's work under licences "
                "their authors set.\n\n"
                f"Underlying error: {exc}"
            )
        return (
            "Could not load libmpv.\n\n"
            f"Put 'mpv-2.dll' (also called 'libmpv-2.dll') in:\n    {root}\n\n"
            "The bundled setup script downloads it for you:\n"
            "    powershell -ExecutionPolicy Bypass -File setup.ps1\n\n"
            "Or grab a build manually from https://sourceforge.net/projects/"
            "mpv-player-windows/files/libmpv/ , extract it, and copy\n"
            "'libmpv-2.dll' next to ChuuTube2Spout, renamed to 'mpv-2.dll'.\n\n"
            f"Underlying error: {exc}"
        )
    return (
        "Could not load libmpv.\n\n"
        "Install it with your package manager, e.g.\n"
        "    sudo apt install libmpv2      (or libmpv1 on older systems)\n"
        "    sudo pacman -S mpv\n\n"
        f"Underlying error: {exc}"
    )
