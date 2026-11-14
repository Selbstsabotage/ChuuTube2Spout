# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate native dependencies BEFORE anything imports the `mpv` module.

python-mpv resolves libmpv through ctypes.util.find_library(), which on Windows
only searches %PATH%. So we prepend our own lib directories to PATH (and register
them as DLL directories for Python 3.8+ dependent-DLL resolution) here, and this
module must be imported before `import mpv` anywhere in the app.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Directories that may hold mpv-2.dll / libmpv-2.dll, relative to the project root.
_LIB_SUBDIRS = ("", "lib", "bin")


# Where the macOS app keeps everything it writes (1.1). An app bundle is
# signed and usually sits in /Applications: nothing may be written into it.
MAC_DATA_DIR = "~/Library/Application Support/ChuuTubePoi"
# What the app bundle carries for the first-run setup, next to the code.
SETTINGS_TEMPLATE = "settings.txt.example"


def is_mac_app(executable: str | None = None,
               frozen: bool | None = None) -> bool:
    """True when running as the macOS app (ChuuTubePoi.app)."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    exe = Path(executable or sys.executable)
    return (frozen and exe.parent.name == "MacOS"
            and exe.parent.parent.name == "Contents"
            and exe.parent.parent.parent.suffix == ".app")


def bundled_dir() -> Path:
    """Where files that travel with the app are: setup.sh, checksums.txt.

    The project root from source and in the Windows release, the bundle's
    own folder (PyInstaller's _MEIPASS) in the macOS app.
    """
    if is_mac_app():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return project_root()


ICON = Path("assets") / "icon.png"


def icon_file() -> Path | None:
    """The app's icon for its windows (and the Dock, run from source).

    In a build it is where PyInstaller put it (--add-data ... assets);
    from source, in the source tree. None if neither has it.
    """
    places = []
    if getattr(sys, "frozen", False):
        places.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
    places.append(Path(__file__).resolve().parent.parent)
    for base in places:
        if (base / ICON).is_file():
            return base / ICON
    return None


def project_root() -> Path:
    """Where the app keeps its files: settings, cache, logs, Node.js.

    From source, the source tree. In the Windows release, the folder holding
    the .exe. In the macOS app, MAC_DATA_DIR - the bundle itself is read-only.
    """
    if is_mac_app():
        return mac_data_dir()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def mac_data_dir() -> Path:
    """MAC_DATA_DIR, created on first use, with a settings.txt in it.

    The settings file is the template the Windows zip ships as settings.txt:
    output size on, everything else off. Never overwritten.
    """
    root = Path(os.path.expanduser(MAC_DATA_DIR))
    try:
        root.mkdir(parents=True, exist_ok=True)
        settings = root / "settings.txt"
        template = Path(getattr(sys, "_MEIPASS", "")) / SETTINGS_TEMPLATE
        if not settings.exists() and template.is_file():
            shutil.copyfile(template, settings)
    except OSError:
        pass
    return root


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
    # Node.js lives in a folder of its own (setup unpacks the whole release
    # there, npm included). PATH only: it holds no DLL anything should load.
    node_dir = root / NODE_DIR
    if node_dir.is_dir():
        os.environ["PATH"] = str(node_dir) + os.pathsep + os.environ.get("PATH", "")
        added.append(node_dir)
    if sys.platform == "darwin":
        added += _prepare_macos_libs(root)
    return added


# Where Homebrew puts libmpv.dylib: /opt/homebrew on Apple Silicon,
# /usr/local on Intel.
HOMEBREW_LIB_DIRS = ("/opt/homebrew/lib", "/usr/local/lib")
# dyld's own fallback list, used when DYLD_FALLBACK_LIBRARY_PATH is unset.
# Setting the variable replaces the list, so it is kept at the end.
_DYLD_DEFAULTS = ("~/lib", "/usr/local/lib", "/lib", "/usr/lib")


def _prepare_macos_libs(root: Path) -> list[Path]:
    """Let python-mpv find Homebrew's libmpv.dylib.

    python-mpv calls ctypes.util.find_library("mpv"). On macOS that searches
    DYLD_FALLBACK_LIBRARY_PATH, read from the environment at the moment of the
    call, or dyld's defaults - which include /usr/local/lib (Intel Homebrew)
    but not /opt/homebrew/lib (Apple Silicon Homebrew). So the Homebrew
    folders go first. Only this process's search is affected: macOS strips
    DYLD_ variables from the programs it starts.
    """
    dirs = [str(root / "lib"), *HOMEBREW_LIB_DIRS]
    present = [d for d in dirs if Path(d).is_dir()]
    current = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    tail = current.split(os.pathsep) if current else \
        [os.path.expanduser(d) for d in _DYLD_DEFAULTS]
    merged = []
    for d in present + tail:
        if d and d not in merged:
            merged.append(d)
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(merged)
    return [Path(d) for d in present]


def gl_profile(platform: str | None = None) -> tuple[int, int, str]:
    """(major, minor, profile) of the OpenGL context to ask Qt for.

    3.3 compatibility everywhere but macOS, where a compatibility request
    only ever gets legacy OpenGL 2.1. There it is 4.1 core, the newest macOS
    has. Every GL call the app makes exists in core profile (FBOs, textures,
    blit, scissor, clear) - checked for 1.1, and libmpv supports it.
    """
    if (platform or sys.platform) == "darwin":
        return 4, 1, "core"
    return 3, 3, "compatibility"


def apply_gl_profile(fmt, platform: str | None = None) -> None:
    """Set gl_profile() on a QSurfaceFormat."""
    from PySide6.QtGui import QSurfaceFormat
    major, minor, profile = gl_profile(platform)
    fmt.setVersion(major, minor)
    fmt.setProfile(QSurfaceFormat.CoreProfile if profile == "core"
                   else QSurfaceFormat.CompatibilityProfile)


# Where setup.ps1 unpacks Node.js, relative to the project root. The whole
# release goes there - node.exe and npm - because npm is needed after setup
# too: the app rebuilds the token generator itself if its build is missing.
NODE_DIR = "node"

# The token generator's package.json asks for Node 22 or newer, and so does
# the yt-dlp plugin before it will use Node at all.
NODE_MIN_MAJOR = 22


def find_node() -> str | None:
    """The Node.js executable: the copy setup installed first, then PATH.

    Our own copy wins over one on PATH. A developer's system Node can be any
    version, and the plugin silently ignores one older than NODE_MIN_MAJOR.
    """
    root = project_root()
    for candidate in (root / NODE_DIR / "node.exe",          # Windows zip
                      root / NODE_DIR / "bin" / "node",      # macOS/Linux tarball
                      root / "node.exe"):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("node")


def parse_node_major(version_line: str) -> int | None:
    """22 from "v22.23.4". None if the line is not a Node version."""
    match = re.match(r"\s*v(\d+)\.", version_line or "")
    return int(match.group(1)) if match else None


_NODE_MAJORS: dict[str, int | None] = {}


def node_major(node: str | None) -> int | None:
    """The major version of the Node.js at `node`, asked once per path.

    Needed because Node's flags change between versions: --allow-net exists
    from Node 25 on, and an older Node refuses to start when given it.
    """
    if not node:
        return None
    if node not in _NODE_MAJORS:
        try:
            done = subprocess.run([node, "--version"], capture_output=True,
                                  text=True, timeout=20,
                                  creationflags=getattr(subprocess,
                                                        "CREATE_NO_WINDOW", 0))
            _NODE_MAJORS[node] = parse_node_major(done.stdout)
        except Exception:
            _NODE_MAJORS[node] = None
    return _NODE_MAJORS[node]


def npm_cli(node: str | None) -> Path | None:
    """npm's own entry script beside `node`, or None.

    Run as `node npm-cli.js ...` rather than through npm.cmd: a .cmd needs a
    shell to start, and a shell is one more layer for an argument to be
    re-parsed by. Two layouts: the Windows zip keeps npm in
    node_modules/npm beside node.exe; the macOS and Linux tarballs keep it in
    lib/node_modules/npm, one folder up from bin/node.
    """
    if not node:
        return None
    here = Path(node).resolve().parent
    for candidate in (here / "node_modules" / "npm" / "bin" / "npm-cli.js",
                      here.parent / "lib" / "node_modules" / "npm" / "bin"
                      / "npm-cli.js"):
        if candidate.is_file():
            return candidate
    return None


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
            # Named as found: yt-dlp.exe on Windows, yt-dlp on a Mac (whose
            # script names the folder it was installed in, so a moved folder
            # breaks it - tester, 1.0.11, running from the Trash).
            print(f"[chuutube] {Path(exe).name} would not start (the folder "
                  "was probably moved or renamed) - using 'python -m yt_dlp' "
                  "instead", file=sys.stderr)
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
                "It fetches libmpv, Node.js and the PO token generator from the "
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
            "'libmpv-2.dll' next to ChuuTubePoi, renamed to 'mpv-2.dll'.\n\n"
            f"Underlying error: {exc}"
        )
    if sys.platform == "darwin":
        again = ("Then open ChuuTubePoi again." if is_mac_app()
                 else "Then start the app again with run.sh.")
        return (
            "Could not load libmpv.\n\n"
            "Install it with Homebrew (https://brew.sh):\n"
            "    brew install mpv\n\n"
            f"{again}\n\n"
            f"Underlying error: {exc}"
        )
    return (
        "Could not load libmpv.\n\n"
        "Install it with your package manager, e.g.\n"
        "    sudo apt install libmpv2      (or libmpv1 on older systems)\n"
        "    sudo pacman -S mpv\n\n"
        f"Underlying error: {exc}"
    )
