# SPDX-License-Identifier: GPL-3.0-or-later
"""Environment diagnostics: `python run.py --doctor`.

Answers, in order, the questions that actually go wrong: is libmpv loadable, is
Spout present, is yt-dlp findable and current, and does this machine give us an
OpenGL context good enough to render into.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .bootstrap import (NODE_MIN_MAJOR, apply_gl_profile, find_node,
                        find_ytdlp, npm_cli, prepare_native_paths,
                        project_root, ytdlp_command)

OK = "  ok   "
BAD = " FAIL  "
WARN = " warn  "


# Parses "v22.23.4" -> 22. One parser, in bootstrap, for the app and this.
from .bootstrap import parse_node_major as node_major  # noqa: E402


def _where(command: list[str]) -> str:
    """A human-readable name for an argv prefix."""
    if len(command) > 1 and command[1] == "--yt-dlp":
        return "bundled inside this build"
    if len(command) > 1:
        return " ".join(command)
    return command[0]


def _line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f"  -  {detail}" if detail else ""))


def run() -> int:
    failures = 0
    print(f"{APP_NAME} diagnostics")
    print("=" * 62)
    _line(OK, "version", __version__)
    _line(OK, "platform", f"{platform.system()} {platform.release()} ({platform.machine()})")
    _line(OK, "python", f"{sys.version.split()[0]}  @ {sys.executable}")
    _line(OK, "app directory", str(project_root()))

    added = prepare_native_paths()
    _line(OK, "native lib search", ", ".join(str(a) for a in added) or "(none)")

    # --- Qt -----------------------------------------------------------------
    try:
        import PySide6
        from PySide6.QtCore import qVersion
        _line(OK, "PySide6", f"{PySide6.__version__} (Qt {qVersion()})")
    except Exception as exc:
        failures += 1
        _line(BAD, "PySide6", str(exc))

    try:
        import OpenGL
        _line(OK, "PyOpenGL", OpenGL.__version__)
    except Exception as exc:
        failures += 1
        _line(BAD, "PyOpenGL", str(exc))

    # --- libmpv -------------------------------------------------------------
    try:
        import mpv
        m = mpv.MPV(vo="null", ao="null", terminal=False, idle=True)
        try:
            version = m.mpv_version
        except Exception:
            version = "unknown"
        _line(OK, "libmpv", str(version))
        m.terminate()
    except Exception as exc:
        failures += 1
        _line(BAD, "libmpv", str(exc))
        if sys.platform == "win32":
            print("        -> put mpv-2.dll next to the app (setup.ps1 does this)")
        elif sys.platform == "darwin":
            print("        -> brew install mpv")

    # --- Spout / Syphon -----------------------------------------------------
    if sys.platform == "darwin":
        try:
            from .syphon_out import SYPHON_AVAILABLE, SYPHON_ERROR
            if SYPHON_AVAILABLE:
                from importlib.metadata import version as _v
                try:
                    ver = _v("syphon-python")
                except Exception:
                    ver = "installed"
                from .config import SYPHON_SERVER
                _line(OK, "Syphon", f"syphon-python {ver}, "
                      f"{SYPHON_SERVER} server (SYPHON_SERVER)")
            else:
                failures += 1
                _line(BAD, "Syphon", SYPHON_ERROR)
        except Exception as exc:
            failures += 1
            _line(BAD, "Syphon", str(exc))
        from .macos import keep_awake_available
        ok, detail = keep_awake_available()
        _line(OK if ok else WARN, "App Nap opt-out", detail)
    elif sys.platform != "win32":
        _line(WARN, "SpoutGL", "not applicable - Spout is Windows-only")
    else:
        try:
            import SpoutGL
            _line(OK, "SpoutGL", getattr(SpoutGL, "__version__", "installed"))
        except Exception as exc:
            failures += 1
            _line(BAD, "SpoutGL", str(exc))

    # --- yt-dlp -------------------------------------------------------------
    # ytdlp_command(), not find_ytdlp(). They disagree in a frozen build: the
    # app re-enters its own exe (which carries yt-dlp and the PO token plugin
    # inside it), while find_ytdlp() falls through to whatever `where yt-dlp`
    # turns up. Reporting the latter would name a copy the app never runs -
    # wrong version, no plugin - and on a clean machine it would cry FAIL over
    # a build that works perfectly.
    ytdlp = ytdlp_command()
    if ytdlp:
        try:
            out = subprocess.run([*ytdlp, "--version"], capture_output=True,
                                 text=True, timeout=60)
            _line(OK, "yt-dlp", f"{out.stdout.strip()}  @ {_where(ytdlp)}")
        except Exception as exc:
            failures += 1
            _line(BAD, "yt-dlp", f"{_where(ytdlp)} would not run: {exc}")
    else:
        failures += 1
        _line(BAD, "yt-dlp", "not found on PATH - YouTube URLs will not load")
        stray = find_ytdlp()
        if stray:
            print(f"        -> there is one at {stray}, but this build cannot "
                  "reach it")

    # --- JavaScript runtime -------------------------------------------------
    # yt-dlp needs one to solve YouTube's signature challenges. Without it,
    # extraction falls back to clients that now demand a PO token and 403.
    # The token generator runs on the same Node.
    node = find_node()
    if node:
        try:
            out = subprocess.run([node, "--version"], capture_output=True,
                                 text=True, timeout=20)
            first = (out.stdout or out.stderr).strip().splitlines()[0]
            major = node_major(first)
            if major is not None and major < NODE_MIN_MAJOR:
                failures += 1
                _line(BAD, "JS runtime (Node.js)",
                      f"{first} is too old, {NODE_MIN_MAJOR} or newer is "
                      f"needed  @ {node}")
                print("        -> re-run setup.ps1, which installs Node.js "
                      "in the folder 'node'")
            else:
                _line(OK, "JS runtime (Node.js)", f"{first}  @ {node}")
        except Exception as exc:
            _line(WARN, "JS runtime (Node.js)", f"found but would not run: {exc}")
        if npm_cli(node) is None:
            _line(WARN, "npm", f"not found beside {node} - the app cannot "
                  "rebuild the token generator by itself")
    else:
        failures += 1
        _line(BAD, "JS runtime (Node.js)", "not found - YouTube formats will 403")
        print("        -> re-run setup.ps1, which installs Node.js in the "
              "folder 'node'")

    # Before 1.1 the runtime was deno. A deno.exe left behind does no harm -
    # yt-dlp is told to use Node only - but it is 100 MB of nothing.
    stale_deno = project_root() / "deno.exe"
    if stale_deno.is_file():
        _line(OK, "deno.exe", "no longer used since 1.1 - you can delete it")

    # --- does yt-dlp itself see the runtime? --------------------------------
    # "Node is installed" and "yt-dlp is using Node" are different claims, and
    # only the second one matters.
    if ytdlp and node:
        try:
            helptext = subprocess.run([*ytdlp, "--help"], capture_output=True,
                                      text=True, timeout=90).stdout
            if "--no-js-runtimes" in helptext:
                _line(OK, "yt-dlp <- Node.js",
                      f"will be passed explicitly as node:{node}")
            else:
                failures += 1
                _line(BAD, "yt-dlp <- Node.js",
                      "this yt-dlp cannot be told to use Node.js "
                      "(no --no-js-runtimes option) - update yt-dlp")
        except Exception as exc:
            _line(WARN, "yt-dlp <- Node.js", f"could not ask yt-dlp: {exc}")

    # --- PO token provider ---------------------------------------------------
    # The single most consequential check here: without one, YouTube serves the
    # first few MB of a video and answers 403 to the rest.
    # All from one place, and all before first use. A module-level import of
    # versions_agree plus this function's own later import of the same name
    # made it a local for the whole of run() - so the earlier call raised
    # UnboundLocalError, in a diagnostic tool, at the point it was diagnosing.
    from .potprovider import (built_script, find_executable,
                              find_script_home, get_provider,
                              plugin_version, script_probe,
                              server_version, versions_agree)
    # What the app will actually use, asked in the order the app asks it. This
    # block used to assume script mode always won, which since 0.9.2 it does
    # not - and it reported a perfectly working token server as a broken script
    # mode, with a red FAIL and "will not work correctly yet".
    provider = get_provider()
    script_home = find_script_home()
    pot_exe = find_executable()
    started_here = not provider.running and provider.start()
    if not provider.running:
        provider.wait_settled()
    server_up = provider.running

    if server_up:
        kind = provider.server_kind or "unknown"
        _line(OK, "PO token provider", f"{kind} server - {provider.status}")
        if kind == "node":
            print("        -> the generator's own server, from the same release as")
            print("           the plugin, so the two cannot disagree on protocol")
    elif script_home:
        _line(OK, "PO token provider", f"script mode: {script_home}")
        print("        -> a token is minted per request; the server would be faster")
    else:
        failures += 1
        _line(BAD, "PO token provider", "nothing can mint a PO token")
        print("        -> without one, YouTube refuses everything past the first")
        print("           few MB of a video, and downloads stall part-way")
        print("        -> re-run setup.ps1")

    # Script mode as the spare. Run it rather than looking for node_modules: a
    # release once shipped a node_modules that existed, weighed half a gigabyte
    # and resolved nothing, because zipping had dereferenced deno's symlinks.
    # The same holds for build/: a file being there is not the same as it
    # running.
    if script_home:
        from pathlib import Path as _P
        script_ready, detail = script_probe(script_home)
        if script_ready:
            _line(OK, "token generator (script mode)", f"ready: {script_home}")
        elif not (_P(script_home) / "node_modules").is_dir() \
                or built_script(script_home) is None:
            _line(WARN, "token generator (script mode)",
                  "present, not built yet")
            print("        -> the app builds it on first launch (a few "
                  "minutes, needs the network)")
            print("           or re-run setup.ps1")
        else:
            # Only fatal when it is the only thing that could issue a token.
            if server_up:
                _line(WARN, "token generator (script mode)",
                      f"not usable as a spare: {detail}")
            else:
                failures += 1
                _line(BAD, "token generator (script mode)", f"broken: {detail}")
            print("        -> delete node_modules in " + script_home)
            print("           and relaunch; the app will rebuild it")

    # The standalone executable, and whether it could stand in. Only worth a
    # word when it is actually the thing in use, or when nothing else is.
    if pot_exe and provider.server_kind != "exe":
        srv, plug = server_version(pot_exe), plugin_version()
        if server_up or script_home:
            _line(OK, "bgutil-pot.exe",
                  f"v{srv} - spare, not in use" if versions_agree(plug, srv)
                  else f"v{srv} - cannot stand in for plugin v{plug}, not in use")
        elif not versions_agree(plug, srv):
            failures += 1
            _line(BAD, "bgutil-pot.exe",
                  f"v{srv} vs plugin v{plug} - the plugin will refuse it")
    elif not pot_exe and not server_up and not script_home:
        print("        -> bgutil-pot.exe not found either")

    if started_here:
        provider.stop()

    from .config import SETTINGS_FILE
    if SETTINGS_FILE:
        _line(OK, "settings file", SETTINGS_FILE)
    else:
        _line(OK, "settings file", "none - every setting at its default")

    # --- download cache ------------------------------------------------------
    from .config import (CACHE_DIR, CACHE_MANIFEST, CACHE_MAX_GB,
                         CACHE_MIN_AGE_MIN, CLEAR_CACHE_ON_EXIT,
                         SINGLE_PASS_AUDIO)
    from pathlib import Path as _CP
    cache = _CP(CACHE_DIR)
    if cache.is_dir():
        clips = [f for f in cache.iterdir()
                 if f.is_file() and not f.name.startswith(".manifest-")]
        remembered = len(list(cache.glob(".manifest-*.json")))
        size = sum(f.stat().st_size for f in clips) / (1024 ** 2)
        _line(OK, "cache", f"{len(clips)} file(s), {size:.0f} MB, "
                           f"{remembered} remembered  @ {cache}")
        if CACHE_MAX_GB > 0:
            pct = size / (CACHE_MAX_GB * 1024) * 100
            _line(OK, "cache limit",
                  f"{CACHE_MAX_GB:g} GB ({pct:.0f}% used) - past that the least "
                  f"recently used go, except anything under "
                  f"{CACHE_MIN_AGE_MIN:g} min old or currently playing")
        else:
            _line(WARN, "cache limit",
                  "none - nothing is deleted while running "
                  "(set CACHE_MAX_GB in settings.txt)")
        _line(OK, "cache on exit",
              "emptied when the app closes" if CLEAR_CACHE_ON_EXIT else
              "kept (CLEAR_CACHE_ON_EXIT=0) - clips persist between sessions")
    else:
        _line(OK, "cache", f"empty ({cache})")
    _line(OK, "load speed", f"remember fetches: {'on' if CACHE_MANIFEST else 'off'}, "
                            f"one pass for video+audio: "
                            f"{'on' if SINGLE_PASS_AUDIO else 'off'}")

    # --- cookies -------------------------------------------------------------
    from .config import (COOKIE_DOMAINS, COOKIE_EXPORT, COOKIE_MAX_AGE_MIN,
                         COOKIES_FROM_BROWSER)
    from .cookies import export as export_cookies
    from .cookies import profile_path, sweep_stale
    from .cookies import resolve as resolve_cookies
    from .cookies import check as login_check
    signed_in, problem = login_check(COOKIES_FROM_BROWSER)
    _line(OK if signed_in else WARN, "YouTube login",
          "signed in" if signed_in else problem)
    if not COOKIES_FROM_BROWSER:
        _line(OK, "cookies", "not configured (set COOKIES_FROM_BROWSER "
                             "in settings.txt)")
    else:
        resolved, warning = resolve_cookies(COOKIES_FROM_BROWSER)
        if resolved:
            _line(OK, "cookies", f"{COOKIES_FROM_BROWSER} -> {resolved}")
        else:
            _line(WARN, "cookies", warning or "could not resolve")

        # The export is what lets the browser stay open, so prove it works here
        # rather than at the moment somebody is trying to load a clip.
        profile = profile_path(COOKIES_FROM_BROWSER)
        if not COOKIE_EXPORT:
            _line(WARN, "cookie export", "disabled - yt-dlp will read the live "
                                         "store, so close the browser first")
        elif profile is None:
            _line(OK, "cookie export", "not a Firefox-family profile - yt-dlp "
                                       "reads this browser itself")
        else:
            # Into a temp directory that is deleted immediately. A diagnostic
            # has no business leaving a live login on disk - and running
            # doctor.bat is the first thing the README tells people to do, so
            # this used to hand a plaintext session to users who had not yet
            # loaded a single video.
            import tempfile
            workdir = _CP(tempfile.mkdtemp(prefix="chuutube-doctor-"))
            try:
                exported, warning = export_cookies(
                    COOKIES_FROM_BROWSER, workdir / "cookies.txt",
                    COOKIE_DOMAINS)
                if exported is None:
                    failures += 1
                    _line(BAD, "cookie export", warning or "failed")
                else:
                    try:
                        lines = exported.read_text(
                            encoding="utf-8", errors="replace").splitlines()
                        count = sum(1 for line in lines
                                    if line and not line.startswith("#"))
                        detail = f"{count} cookie(s) for " \
                                 f"{', '.join(COOKIE_DOMAINS)}"
                    except OSError as exc:
                        detail = f"exported, but could not re-read it: {exc}"
                    _line(OK, "cookie export",
                          f"{detail}; kept in a temp file for the session only, "
                          "never in this folder")
            finally:
                import shutil as _sh
                _sh.rmtree(workdir, ignore_errors=True)

    removed = sweep_stale()
    if removed:
        _line(OK, "temp cleanup", f"removed {removed} leftover cookie "
                                  "snapshot(s) from a previous crash")

    # --- screens -------------------------------------------------------------
    # Here because "the window opened wider than my monitor" is otherwise a
    # conversation, and the answer is one line of geometry.
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication as _QApp
        _QApp.instance() or _QApp([])
        from .config import WINDOW_SCALE
        for screen in QGuiApplication.screens():
            geo, avail = screen.geometry(), screen.availableGeometry()
            _line(OK, "screen",
                  f"{screen.name()}  {geo.width()}x{geo.height()} "
                  f"({avail.width()}x{avail.height()} usable, "
                  f"{screen.devicePixelRatio():g}x scaling)")
        _line(OK, "window scale",
              f"{WINDOW_SCALE:g} -> wants about "
              f"{int(round(1320 * WINDOW_SCALE))}px wide, capped to fit")
    except Exception as exc:
        _line(WARN, "screens", f"could not enumerate: {exc}")

    # --- OpenGL context -----------------------------------------------------
    try:
        from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        fmt = QSurfaceFormat()
        apply_gl_profile(fmt)
        surface = QOffscreenSurface()
        surface.setFormat(fmt)
        surface.create()
        ctx = QOpenGLContext()
        ctx.setFormat(fmt)
        if ctx.create() and ctx.makeCurrent(surface):
            from OpenGL import GL
            vendor = GL.glGetString(GL.GL_VENDOR).decode()
            renderer = GL.glGetString(GL.GL_RENDERER).decode()
            version = GL.glGetString(GL.GL_VERSION).decode()
            _line(OK, "OpenGL", f"{version} | {renderer} | {vendor}")
            major = ctx.format().majorVersion()
            if major < 3:
                failures += 1
                _line(BAD, "OpenGL version", "need 3.0+ for framebuffer blitting")
            ctx.doneCurrent()
        else:
            failures += 1
            _line(BAD, "OpenGL", "could not create a context")
        del app
    except Exception as exc:
        failures += 1
        _line(BAD, "OpenGL", str(exc))

    print("=" * 62)
    if failures:
        print(f"{failures} problem(s) found. {APP_NAME} will not work correctly yet.")
    else:
        print("All good. Launch with run.bat (Windows) or `python run.py`.")
    return 1 if failures else 0
