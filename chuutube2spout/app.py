# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point. Order matters here: native paths, then Qt attributes, then imports."""

from __future__ import annotations

import sys

from .bootstrap import (YTDLP_FLAG, mpv_import_error_help,
                        prepare_native_paths)


def expire_cache() -> None:
    """Drop cached clips past CACHE_MAX_AGE_H. Quiet when there is nothing to do.

    Separate from clear_cache (which is the --clear-cache flag and empties the
    lot) because this one is the routine housekeeping the app does for itself.
    """
    from pathlib import Path
    from .config import CACHE_DIR, CACHE_MAX_AGE_H

    if CACHE_MAX_AGE_H <= 0:
        return
    cache = Path(CACHE_DIR)
    if not cache.is_dir():
        return
    from .downloader import prune
    # Size limit off: at startup nothing is playing, but the size sweep is the
    # job of the download path, which knows what it just wrote. Age only here.
    prune(cache, max_gb=0, max_age_hours=CACHE_MAX_AGE_H)


def clear_cache() -> int:
    """Empty the download cache, wherever it has been configured to live.

    Here rather than in clear-cache.bat because the location is configurable.
    A batch file that hardcoded "cache" would print "Done." while leaving a
    relocated cache - and the record of what has been played from it - untouched.
    """
    import shutil
    from pathlib import Path
    from .config import CACHE_DIR

    cache = Path(CACHE_DIR)
    if not cache.is_dir():
        print(f"Nothing cached ({cache})")
        return 0
    files = [f for f in cache.iterdir() if f.is_file()]
    size = sum(f.stat().st_size for f in files) / (1024 ** 2)
    print(f"Deleting {len(files)} file(s), {size:.0f} MB from {cache}")
    shutil.rmtree(cache, ignore_errors=True)
    if cache.exists():
        print("Some files could not be removed - is the app still running?")
        return 1
    print("Done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    # Must be the very first thing: a frozen build runs yt-dlp by re-launching
    # itself with this flag, and that child must never touch Qt, mpv or Spout.
    if len(argv) > 1 and argv[1] == YTDLP_FLAG:
        # The frozen interpreter does not reliably take PYTHONUTF8 from the
        # environment, and on a cp1252 machine the child's streams came up in
        # cp1252. downloader.FORCE_UTF8 fixes what yt-dlp itself writes; this
        # covers everything else that reaches the pipe - a traceback from
        # inside yt-dlp included - so the parent's UTF-8 decode is always right.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        from yt_dlp import main as ytdlp_main
        return ytdlp_main(argv[2:])
    # Before anything else that might print. Everything the app says about
    # itself goes to stderr, which the exe throws away the moment its console
    # is closed - so a tester who hits a problem has nothing to send. Not for
    # the yt-dlp child above (it would write a log per download) and not for
    # the console tools below (they are run from a .bat, with a console right
    # there, and a run of check.bat would age out the GUI log people want).
    if not any(a in argv for a in ("--doctor", "--check", "--version",
                                   "--clear-cache")):
        try:
            from .logfile import start as start_logging
            start_logging()
        except Exception as exc:                # never fatal - it is a log
            print(f"[chuutube] logging is off ({exc})", file=sys.stderr)

    if "--doctor" in argv:
        from .doctor import run as run_doctor
        return run_doctor()
    if "--check" in argv:
        prepare_native_paths()
        from .urlcheck import main as check_main
        return check_main([a for a in argv if a != "--check"])
    if "--version" in argv:
        from . import APP_NAME, LICENSE, __version__
        print(f"{APP_NAME} {__version__}")
        print(f"Licence: {LICENSE}. This program comes with ABSOLUTELY NO "
              "WARRANTY.")
        print("Free software: you are welcome to redistribute it under the "
              "terms of the GNU GPL. See LICENSE.")
        return 0
    if "--clear-cache" in argv:
        return clear_cache()

    # Must happen before anything imports the `mpv` module.
    prepare_native_paths()

    # A run that was killed rather than closed can strand a copy of the whole
    # browser cookie store in %TEMP%. Tidy up after the last one before doing
    # anything else.
    try:
        from .cookies import sweep_stale
        sweep_stale()
    except Exception:
        pass

    # Age out the cache on the way in as well as after each download. prune()
    # otherwise only runs when something finishes downloading, so a cache left
    # overnight with "clear on exit" off would sit there until the next clip
    # was fetched - which is exactly when the user is least interested in
    # waiting for disk work.
    try:
        expire_cache()
    except Exception:
        pass

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QSurfaceFormat
    from PySide6.QtWidgets import QApplication, QMessageBox

    # Both the widget context and any Qt-internal contexts must share resources,
    # and this has to be set before the QApplication exists.
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    # No vsync. QOpenGLWidget's swap happens on the GUI thread, so waiting for
    # the display blocks button presses and slider updates along with it. The
    # preview may tear; nobody is watching the preview for quality, and the
    # Spout output is unaffected either way.
    fmt.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(fmt)

    from . import APP_NAME
    from .config import canvas_problem, mode_problem

    # Before the QApplication, so its own startup messages go through it too.
    from .qtmessages import install as install_qt_messages
    install_qt_messages()

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)

    # Before anything expensive: a mode that cannot work should say so rather
    # than extract, play nothing, and leave the reason in a log nobody is
    # watching. QApplication exists by now, so this can be a dialog.
    problem = mode_problem() or canvas_problem()
    if problem:
        QMessageBox.critical(None, f"{APP_NAME} - unusable setting", problem)
        return 2

    # In the log, because "why is my 4K clip soft" and "why is there a black
    # bar" both have their answer here and neither is visible in the window.
    from .config import SENDER_FIT, canvas, output_description
    width, height = canvas()
    if width and height:
        print(f"[chuutube] Spout canvas: {width}x{height} ({SENDER_FIT})",
              file=sys.stderr)
    else:
        print(f"[chuutube] Spout output: each clip at its own size "
              f"({output_description()})", file=sys.stderr)

    try:
        from .ui import MainWindow
    except (OSError, ImportError) as exc:
        # A release ships no libmpv on purpose, so this is the expected first
        # run rather than a fault. Offer to fetch it here: asking someone to
        # find a .bat before the thing they downloaded will open is a poor
        # first impression, and they already have the window open.
        from .firstrun import offer_runtime_install
        if not offer_runtime_install(exc):
            QMessageBox.critical(None, f"{APP_NAME} - missing dependency",
                                 mpv_import_error_help(exc))
            return 2
        try:
            from .ui import MainWindow
        except (OSError, ImportError) as exc:
            # Installed something, still cannot load it. Do not loop - say so.
            QMessageBox.critical(None, f"{APP_NAME} - missing dependency",
                                 mpv_import_error_help(exc))
            return 2

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
