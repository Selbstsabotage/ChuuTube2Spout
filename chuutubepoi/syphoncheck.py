# SPDX-License-Identifier: GPL-3.0-or-later
"""--syphon-check: does Syphon carry transparency on this Mac?

K, 1.0.4: a 4:3 clip in the 16:9 canvas reached Resolume with black bars on
the Mac, where on Windows (Spout) the same bars are transparent. Our frame
clears the bars to alpha 0 the same way on both. So this sends a test frame
through Syphon and receives it back in the same process, with a Syphon client:

    left half   transparent (0, 0, 0, 0)
    right half  opaque red  (255, 0, 0, 255)

If the left half arrives with alpha 0, Syphon keeps transparency and the
bars turn black on the receiving side (Resolume). If it arrives opaque,
Syphon - or how we publish to it - loses it.

Not part of the app's normal start. Run from Terminal:

    bash run.sh --syphon-check                     loopback: the OpenGL server
                                                   unflipped and flipped, and
                                                   the Metal path the app uses
    bash run.sh --syphon-check ChuuTubePoi-B   the running app's source B,
                                                   read from this second process
"""

from __future__ import annotations

import os
import sys
import time

W, H = 64, 32
# (server, flipped) for each loopback. The app sends flipped, through Metal
# unless SYPHON_SERVER=opengl.
LOOPBACKS = (("opengl", False), ("opengl", True), ("metal", True))
GL_TEXTURE_RECTANGLE = 0x84F5
TIMEOUT_S = 8.0


def _value(obj, name):
    """A PyObjC property: a method on some versions, a value on others."""
    attr = getattr(obj, name)
    return attr() if callable(attr) else attr


# The opaque half is green in its first rows: the TOP of the picture, the way
# the app's texture is laid out with SYPHON_FLIP on. Where the green arrives
# says which way up the receiver gets it (1.0.11).
STRIPE_ROWS = H // 4


def test_pixels() -> bytes:
    clear = bytes((0, 0, 0, 0)) * (W // 2)
    red = clear + bytes((255, 0, 0, 255)) * (W - W // 2)
    green = clear + bytes((0, 255, 0, 255)) * (W - W // 2)
    return green * STRIPE_ROWS + red * (H - STRIPE_ROWS)


def _green(pixel) -> bool:
    return pixel[1] > 200 and pixel[0] < 60


def orientation(top, bottom) -> str | None:
    """'upright', 'upside down' or None (unreadable).

    `top` and `bottom` are the opaque half's pixels in the received
    texture's last and first row. A Syphon client hands over a texture the
    GL way - its last row is the top of the picture - and that is how
    Resolume draws it.
    """
    if _green(top) and not _green(bottom):
        return "upright"
    if _green(bottom) and not _green(top):
        return "upside down"
    return None


def orientation_summary(found: dict) -> tuple[bool, str]:
    """(ok, sentence): is the app's path the same way up as the OpenGL one?

    `found` maps (server, flipped) to orientation(). The OpenGL server, sent
    flipped, was upright in Resolume (tester, 1.0.3); the Metal path, sent
    the same way, must match it.
    """
    reference = found.get(("opengl", True))
    metal = found.get(("metal", True))
    if reference is None or metal is None:
        return False, ("which way up could not be read "
                       f"(OpenGL: {reference}, Metal: {metal})")
    if reference == metal:
        return True, (f"the Metal path arrives {metal}, the same as the "
                      "OpenGL server - right way up in Resolume")
    return False, (f"the Metal path arrives {metal}, the OpenGL server "
                   f"{reference}: the Metal path is turned over")


def verdict(left, right) -> tuple[bool, str]:
    """(transparency survives, sentence) from two received RGBA pixels."""
    if right is None or right[3] < 200:
        return False, ("the received frame is not usable (the opaque half "
                       f"came back as {right}) - the test itself failed")
    if left[3] <= 16:
        return True, ("Syphon keeps transparency: the transparent half "
                      f"arrived with alpha {left[3]}. The black bars come "
                      "from the receiving side (Resolume), not from this app.")
    return False, ("Syphon loses transparency: the transparent half arrived "
                   f"as {left}, alpha {left[3]} instead of 0.")


def _read_pixel(GL, texture, target, x, y):
    fbo = GL.glGenFramebuffers(1)
    try:
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                  target, texture, 0)
        data = GL.glReadPixels(x, y, 1, 1, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        return tuple(bytes(data)[:4])
    finally:
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        GL.glDeleteFramebuffers(1, [fbo])


def _gl_context():
    """(app, context, surface, GL) with an OpenGL context current, or raise."""
    from PySide6.QtGui import (QGuiApplication, QOffscreenSurface,
                               QOpenGLContext, QSurfaceFormat)
    from .bootstrap import apply_gl_profile
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    fmt = QSurfaceFormat()
    apply_gl_profile(fmt)
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create():
        raise RuntimeError("could not create an OpenGL context")
    surface = QOffscreenSurface()
    surface.setFormat(ctx.format())
    surface.create()
    ctx.makeCurrent(surface)
    fmt = ctx.format()
    print(f"OpenGL {fmt.majorVersion()}.{fmt.minorVersion()} "
          f"{'core' if fmt.profile() == QSurfaceFormat.CoreProfile else 'compatibility'}")
    from OpenGL import GL
    return app, ctx, surface, GL


def _receive(app, ctx, surface, name, publish=None):
    """Find the Syphon server called `name`, take one frame from it.

    Returns (client, texture, width, height). `publish` is called on every
    pass, for a server in this same process; one in another process (the
    running app) publishes by itself. The client must be stopped by the
    caller, after reading.
    """
    import syphon
    from syphon.server_directory import SyphonServerDirectory
    directory = SyphonServerDirectory()
    found = []
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline and not found:
        if publish:
            publish()
        app.processEvents()
        ctx.makeCurrent(surface)
        found = directory.servers_matching_name(name=name)
        time.sleep(0.05)
    if not found:
        raise RuntimeError(f"no Syphon server called '{name}' appeared")
    print(f"server    found: {_value(found[0], 'name')} "
          f"({_value(found[0], 'app_name')})")
    client = syphon.SyphonOpenGLClient(found[0])
    image = None
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline and image is None:
        if publish:
            publish()
        app.processEvents()
        ctx.makeCurrent(surface)
        if client.has_new_frame:
            image = client.new_frame_image
        time.sleep(0.05)
    if image is None:
        client.stop()
        raise RuntimeError("the client never received a frame")
    texture = int(_value(image, "textureName"))
    size = _value(image, "textureSize")
    return (client, texture, int(getattr(size, "width", 0)),
            int(getattr(size, "height", 0)))


def _loopback(app, ctx, surface, GL, server: str, flipped: bool):
    """Our test frame through Syphon and back, sent this way.

    Returns (transparency ok, orientation()).
    """
    from .syphon_out import Sender
    texture = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
    for p in (GL.GL_TEXTURE_MIN_FILTER, GL.GL_TEXTURE_MAG_FILTER):
        GL.glTexParameteri(GL.GL_TEXTURE_2D, p, GL.GL_NEAREST)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, W, H, 0, GL.GL_RGBA,
                    GL.GL_UNSIGNED_BYTE, test_pixels())
    GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
    name = f"ChuuTubePoi-alpha-check-{os.getpid()}-{server}-{int(flipped)}"
    sender = Sender(name, server=server)
    client = None
    print(f"--- loopback, {server} server, sent "
          f"{'flipped' if flipped else 'unflipped'} (the app sends flipped)")
    try:
        client, rx, width, height = _receive(
            app, ctx, surface, name,
            publish=lambda: sender.send(texture, W, H, flipped))
        print(f"received  texture {rx}, {width}x{height}")
        left = _read_pixel(GL, rx, GL_TEXTURE_RECTANGLE, width // 4, height // 2)
        right = _read_pixel(GL, rx, GL_TEXTURE_RECTANGLE, 3 * width // 4,
                            height // 2)
        print(f"received  left {left}  right {right}")
        ok, sentence = verdict(left, right)
        print(("OK    " if ok else "FAIL  ") + sentence)
        top = _read_pixel(GL, rx, GL_TEXTURE_RECTANGLE, 3 * width // 4,
                          height - 2)
        bottom = _read_pixel(GL, rx, GL_TEXTURE_RECTANGLE, 3 * width // 4, 1)
        way = orientation(top, bottom)
        print(f"received  {way or 'unreadable'} (green rows: top {top}, "
              f"bottom {bottom})")
        return ok, way
    finally:
        if client is not None:
            client.stop()
        sender.release()
        GL.glDeleteTextures(1, [texture])


def _external(app, ctx, surface, GL, name: str) -> bool:
    """Read a running server's frame from this separate process.

    The real stream, received the way Resolume receives it: what are the
    left edge and the centre? Start the app first and play a 4:3 clip in
    that slot.
    """
    print(f"--- the running app's source '{name}', from a second process")
    client, rx, width, height = _receive(app, ctx, surface, name)
    try:
        print(f"received  texture {rx}, {width}x{height}")
        edge = _read_pixel(GL, rx, GL_TEXTURE_RECTANGLE, 1, height // 2)
        centre = _read_pixel(GL, rx, GL_TEXTURE_RECTANGLE, width // 2,
                             height // 2)
        print(f"received  left edge {edge}  centre {centre}")
        if edge[3] <= 16:
            print("OK    the bars arrive transparent in another process. "
                  "Whatever shows them black ignores the alpha.")
            return True
        print(f"FAIL  the bars arrive with alpha {edge[3]}: the live stream "
              "loses transparency on the way into Syphon.")
        return False
    finally:
        client.stop()


def _print_settings() -> None:
    """Version and the settings that decide the Syphon output, as in effect."""
    from . import __version__
    from .config import SETTINGS_FILE, SYPHON_FLIP, SYPHON_SERVER
    print(f"ChuuTubePoi {__version__}: SYPHON_FLIP={int(SYPHON_FLIP)}, "
          f"SYPHON_SERVER={SYPHON_SERVER} (settings: {SETTINGS_FILE or 'none'})")


def check_target(argv: list[str]) -> str | None:
    """The server name after --syphon-check, if one was given."""
    if "--syphon-check" not in argv:
        return None
    after = argv[argv.index("--syphon-check") + 1:]
    return after[0] if after and not after[0].startswith("-") else None


def run(argv: list[str] | None = None) -> int:
    """--syphon-check [SERVER]: loopback both ways, or a running source."""
    if sys.platform != "darwin":
        print("--syphon-check is for macOS (Syphon is macOS-only).")
        return 2
    target = check_target(list(sys.argv if argv is None else argv))
    try:
        app, ctx, surface, GL = _gl_context()
    except Exception as exc:
        print(f"FAIL  {exc}")
        return 1
    try:
        if target:
            return 0 if _external(app, ctx, surface, GL, target) else 1
        _print_settings()
        found = {}
        results = []
        for server, flipped in LOOPBACKS:
            ok, way = _loopback(app, ctx, surface, GL, server, flipped)
            results.append(ok)
            found[(server, flipped)] = way
        ok, sentence = orientation_summary(found)
        print("--- which way up")
        print(("OK    " if ok else "FAIL  ") + sentence)
        return 0 if all(results) and ok else 1
    except Exception as exc:
        print(f"FAIL  the check itself broke: {exc!r}")
        return 1
    finally:
        ctx.doneCurrent()


# --- --syphon-pattern (1.0.6) ------------------------------------------------
# The app sends transparent bars and Syphon delivers them (run() above, and
# the app's own edge log) - yet Resolume on the tester's Mac shows them black,
# with any blend mode, in a fresh Avenue. To tell whether Resolume ignores
# alpha from every Syphon sender or only from OpenGL ones, this publishes the
# same test picture several times, until stopped:
#
#   ChuuTubePoi-test-App      the way the app sends now (1.0.8: our copy
#                                into an IOSurface, Syphon's Metal server)
#   ChuuTubePoi-test-OpenGL   the way 1.0.7 and earlier sent
#                                (SyphonOpenGLServer)
#   ChuuTubePoi-test-Metal    Syphon's Metal server, fed directly
#
# K, 1.0.7: Metal showed the layer beneath through the bars in Resolume.
# OpenGL was all black - but that texture was incomplete (see pattern()),
# so it said nothing about OpenGL.
#
# The picture is a 4:3 clip in 16:9: transparent bars left and right, opaque
# red in the middle, with a white stripe along the TOP of the red - upside
# down, the stripe is at the bottom.

PATTERN_W, PATTERN_H = 1920, 1080
PATTERN_BAR = 240                     # 1440x1080 picture in the middle
PATTERN_STRIPE = 120                  # white rows at the top of the picture
PATTERN_SECONDS = 600


def pattern_rows():
    """RGBA rows, top to bottom: bar, red (white in the top rows), bar."""
    bar = bytes((0, 0, 0, 0)) * PATTERN_BAR
    inner = PATTERN_W - 2 * PATTERN_BAR
    red = bar + bytes((255, 0, 0, 255)) * inner + bar
    white = bar + bytes((255, 255, 255, 255)) * inner + bar
    return white * PATTERN_STRIPE + red * (PATTERN_H - PATTERN_STRIPE)


def pattern() -> int:
    if sys.platform != "darwin":
        print("--syphon-pattern is for macOS (Syphon is macOS-only).")
        return 2
    from PySide6.QtGui import (QGuiApplication, QOffscreenSurface,
                               QOpenGLContext, QSurfaceFormat)
    from .bootstrap import apply_gl_profile
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    fmt = QSurfaceFormat()
    apply_gl_profile(fmt)
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create():
        print("FAIL  could not create an OpenGL context")
        return 1
    surface = QOffscreenSurface()
    surface.setFormat(ctx.format())
    surface.create()
    ctx.makeCurrent(surface)

    import numpy as np
    from OpenGL import GL
    import syphon
    from .syphon_out import Sender

    data = pattern_rows()
    texture = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
    # Without these the texture is incomplete (no mipmaps for the default
    # filter): 1.0.7 sent it so, and macOS drew it as black.
    for p in (GL.GL_TEXTURE_MIN_FILTER, GL.GL_TEXTURE_MAG_FILTER):
        GL.glTexParameteri(GL.GL_TEXTURE_2D, p, GL.GL_NEAREST)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, PATTERN_W, PATTERN_H, 0,
                    GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
    GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
    gl_sender = Sender("ChuuTubePoi-test-OpenGL", server="opengl")
    app_sender = Sender("ChuuTubePoi-test-App", server="metal")

    metal_server = None
    metal_texture = None
    try:
        from syphon.utils.numpy import copy_image_to_mtl_texture
        from syphon.utils.raw import create_mtl_texture
        metal_server = syphon.SyphonMetalServer("ChuuTubePoi-test-Metal")
        metal_texture = create_mtl_texture(metal_server.device, PATTERN_W,
                                           PATTERN_H)
        image = np.frombuffer(data, dtype=np.uint8).reshape(
            PATTERN_H, PATTERN_W, 4)
        copy_image_to_mtl_texture(image, metal_texture)
    except Exception as exc:
        print(f"FAIL  the Metal server could not start: {exc!r}")
        metal_server = None

    print("Sending two Syphon test sources for "
          f"{PATTERN_SECONDS // 60} minutes (Ctrl+C stops earlier):")
    print("  ChuuTubePoi-test-App      - how the app sends (1.0.8: Metal)")
    print("  ChuuTubePoi-test-OpenGL   - how 1.0.7 and earlier sent")
    if metal_server is not None:
        print("  ChuuTubePoi-test-Metal    - Syphon's Metal server, direct")
    print("Each is red in the middle with TRANSPARENT bars left and right,")
    print("and a white stripe that belongs at the TOP.")
    print("In Resolume, put each on a layer above something visible. "
          "Note for each: do the bars show the layer beneath, or black?")
    deadline = time.monotonic() + PATTERN_SECONDS
    try:
        while time.monotonic() < deadline:
            ctx.makeCurrent(surface)
            gl_sender.send(texture, PATTERN_W, PATTERN_H, True)
            app_sender.send(texture, PATTERN_W, PATTERN_H, True)
            if metal_server is not None:
                metal_server.publish_frame_texture(metal_texture)
            app.processEvents()
            time.sleep(1 / 30)
    except KeyboardInterrupt:
        pass
    finally:
        ctx.makeCurrent(surface)
        gl_sender.release()
        app_sender.release()
        if metal_server is not None:
            try:
                metal_server.stop()
            except Exception:
                pass
        ctx.makeCurrent(surface)
        GL.glDeleteTextures(1, [texture])
        ctx.doneCurrent()
    print("Stopped.")
    return 0
