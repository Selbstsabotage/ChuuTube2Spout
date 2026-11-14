# SPDX-License-Identifier: GPL-3.0-or-later
"""Syphon servers: the macOS counterpart of spout_out.py.

Syphon shares a GPU texture between apps on macOS, the way Spout does on
Windows. Resolume on a Mac lists each server as a source. The interface is
the one SpoutOutput has, so surface.py and ui.py do not care which one they
were given: create_output() in spout_out.py picks.

The binding is syphon-python (MIT, on PyObjC). There are two ways a frame
gets in (SYPHON_SERVER):

    metal   our texture is copied by GL into an IOSurface that a Metal
            texture shares, and Syphon's Metal server publishes that.
    opengl  Syphon's OpenGL server draws our texture into its own surface.

Both need the preview's GL context current. send() is only ever called from
the render tick, with that context current, so - as with Spout - nothing is
created until the first frame.
"""

from __future__ import annotations

import ctypes
import sys

from .config import SENDER_A, SENDER_B, SYPHON_FLIP, SYPHON_SERVER

try:
    import syphon  # type: ignore

    SYPHON_AVAILABLE = True
    SYPHON_ERROR = ""
except Exception as exc:  # pragma: no cover - platform dependent
    syphon = None  # type: ignore
    SYPHON_AVAILABLE = False
    SYPHON_ERROR = (
        "syphon-python is not available - Syphon output is disabled. "
        + ("Syphon is macOS-only." if sys.platform != "darwin"
           else f"Run setup.sh again. ({exc})")
    )


def _reset_draw_state() -> None:
    """Put back the GL state that decides what Syphon's copy writes.

    Unlike Spout, Syphon's OpenGL server copies our texture by DRAWING it into
    its own surface, in our context, with whatever state that context is in.
    The loopback check (clean context) kept alpha; the live app, publishing
    from the preview's context after mpv and the preview have drawn, showed
    black bars in Resolume and in Syphon Recorder. Blending, a colour mask without alpha, or a
    left-over scissor would each do that. Cheap, so done before every frame.
    """
    try:
        from OpenGL import GL
        GL.glDisable(GL.GL_BLEND)
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glColorMask(GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE)
    except Exception:
        pass


def _flush() -> None:
    """Hand the GL commands so far to the GPU.

    What our context drew is only certain to be seen by another context, or
    by Metal, after a flush.
    """
    try:
        from OpenGL import GL
        GL.glFlush()
    except Exception:
        pass


def blit_rows(height: int, flipped: bool) -> tuple[int, int]:
    """(dst_y0, dst_y1) for copying our texture into the shared IOSurface.

    With SYPHON_FLIP (the default, `flipped`) the copy turns the picture
    over; without it, rows are copied straight. Found by test, not derived:
    K, 1.0.9 - the straight copy came out upside down in Resolume, while the
    same texture through the OpenGL server, sent as flipped, was upright.
    So SYPHON_FLIP means the same for both servers: on is upright.
    """
    return (height, 0) if flipped else (0, height)


# GL constants used by the Metal path, by value: PyOpenGL's core-profile
# namespace does not always export the Apple ones.
GL_TEXTURE_RECTANGLE = 0x84F5
GL_BGRA = 0x80E1
GL_UNSIGNED_INT_8_8_8_8_REV = 0x8367
IOSURFACE_BGRA = 0x42475241            # 'BGRA'
OPENGL_FRAMEWORK = "/System/Library/Frameworks/OpenGL.framework/OpenGL"


class _OpenGLPath:
    """Syphon's OpenGL server: it draws our texture into its own surface."""

    kind = "OpenGL"

    def __init__(self, name: str) -> None:
        self.server = syphon.SyphonOpenGLServer(name)

    def publish(self, texture_id: int, width: int, height: int,
                flipped: bool) -> None:
        _flush()
        self.server.publish_frame_texture(
            texture_id, size=(int(width), int(height)), is_flipped=bool(flipped))

    def stop(self) -> None:
        self.server.stop()


class _MetalPath:
    """Our texture, copied by GL into an IOSurface, published through Metal.

    K, 1.0.8: through the OpenGL server the 4:3 bars arrived opaque in every
    receiver, while our texture held alpha 0 and Syphon's Metal server kept
    a test picture's bars transparent in Resolume. So the copy is ours: a
    glBlitFramebuffer, which writes the pixels as they are - no shader, no
    blending, no context of Syphon's in between. Two surfaces, used in turn,
    so Metal is never reading the one GL is writing.
    """

    kind = "Metal"
    SURFACES = 2

    def __init__(self, name: str) -> None:
        import Metal  # noqa: F401 - fails here, not mid-frame, if missing
        self.server = syphon.SyphonMetalServer(name)
        self._size = None
        self._slots = []       # [(surface, gl_texture, fbo, mtl_texture)]
        self._next = 0
        self._read_fbo = 0
        self._cgl = None

    def _cgl_lib(self):
        if self._cgl is None:
            lib = ctypes.cdll.LoadLibrary(OPENGL_FRAMEWORK)
            lib.CGLGetCurrentContext.restype = ctypes.c_void_p
            lib.CGLGetCurrentContext.argtypes = []
            lib.CGLTexImageIOSurface2D.restype = ctypes.c_int32
            lib.CGLTexImageIOSurface2D.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_int32, ctypes.c_int32, ctypes.c_uint32,
                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
            self._cgl = lib
        return self._cgl

    @staticmethod
    def _new_surface(width: int, height: int):
        import objc
        props = {"IOSurfaceWidth": int(width), "IOSurfaceHeight": int(height),
                 "IOSurfaceBytesPerElement": 4,
                 "IOSurfacePixelFormat": IOSURFACE_BGRA}
        try:
            import IOSurface  # pyobjc-framework-IOSurface
            surface = IOSurface.IOSurfaceCreate(props)
        except Exception:
            surface = objc.lookUpClass("IOSurface").alloc().initWithProperties_(
                props)
        if surface is None:
            raise RuntimeError("IOSurface could not be created")
        return surface, objc.pyobjc_id(surface)

    def _metal_texture(self, surface, width: int, height: int):
        import Metal
        device = self.server.device
        for mode in (None, getattr(Metal, "MTLStorageModeShared", 0),
                     getattr(Metal, "MTLStorageModeManaged", 1)):
            desc = Metal.MTLTextureDescriptor \
                .texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                    Metal.MTLPixelFormatBGRA8Unorm, int(width), int(height),
                    False)
            desc.setUsage_(Metal.MTLTextureUsageShaderRead)
            if mode is not None:
                desc.setStorageMode_(mode)
            texture = device.newTextureWithDescriptor_iosurface_plane_(
                desc, surface, 0)
            if texture is not None:
                return texture
        raise RuntimeError("Metal would not wrap the IOSurface")

    def _allocate(self, width: int, height: int) -> None:
        from OpenGL import GL
        self._free()
        cgl = self._cgl_lib()
        context = cgl.CGLGetCurrentContext()
        if not context:
            raise RuntimeError("no current GL context")
        bound = GL.glGetIntegerv(GL.GL_FRAMEBUFFER_BINDING)
        try:
            for _ in range(self.SURFACES):
                surface, pointer = self._new_surface(width, height)
                texture = int(GL.glGenTextures(1))
                GL.glBindTexture(GL_TEXTURE_RECTANGLE, texture)
                error = cgl.CGLTexImageIOSurface2D(
                    context, GL_TEXTURE_RECTANGLE, GL.GL_RGBA8, int(width),
                    int(height), GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV,
                    ctypes.c_void_p(pointer), 0)
                GL.glBindTexture(GL_TEXTURE_RECTANGLE, 0)
                fbo = int(GL.glGenFramebuffers(1))
                self._slots.append((surface, texture, fbo, None))
                if error:
                    raise RuntimeError(f"CGLTexImageIOSurface2D error {error}")
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
                GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER,
                                          GL.GL_COLOR_ATTACHMENT0,
                                          GL_TEXTURE_RECTANGLE, texture, 0)
                status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
                if status != GL.GL_FRAMEBUFFER_COMPLETE:
                    raise RuntimeError(f"framebuffer status {status:#x}")
                metal = self._metal_texture(surface, width, height)
                self._slots[-1] = (surface, texture, fbo, metal)
            self._read_fbo = int(GL.glGenFramebuffers(1))
        finally:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, int(bound))
        self._size = (width, height)

    def publish(self, texture_id: int, width: int, height: int,
                flipped: bool) -> None:
        from OpenGL import GL
        if self._size != (width, height):
            self._allocate(width, height)
        _surface, _texture, fbo, metal = self._slots[self._next]
        self._next = (self._next + 1) % len(self._slots)
        read = GL.glGetIntegerv(GL.GL_READ_FRAMEBUFFER_BINDING)
        draw = GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING)
        try:
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._read_fbo)
            GL.glFramebufferTexture2D(GL.GL_READ_FRAMEBUFFER,
                                      GL.GL_COLOR_ATTACHMENT0,
                                      GL.GL_TEXTURE_2D, texture_id, 0)
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, fbo)
            y0, y1 = blit_rows(height, flipped)
            GL.glBlitFramebuffer(0, 0, width, height, 0, y0, width, y1,
                                 GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
        finally:
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, int(read))
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, int(draw))
        _flush()
        self.server.publish_frame_texture(metal)

    def _free(self) -> None:
        try:
            from OpenGL import GL
            for _surface, texture, fbo, _metal in self._slots:
                GL.glDeleteFramebuffers(1, [fbo])
                GL.glDeleteTextures([texture])
            if self._read_fbo:
                GL.glDeleteFramebuffers(1, [self._read_fbo])
        except Exception:
            pass
        self._slots = []
        self._read_fbo = 0
        self._next = 0
        self._size = None

    def stop(self) -> None:
        self._free()
        self.server.stop()


class Sender:
    """One named Syphon server, created lazily on the first frame.

    `server` is SYPHON_SERVER unless given ("metal" or "opengl"). If the
    Metal path cannot start or fails, the sender says so once and carries
    on with the OpenGL server for the rest of the session.
    """

    def __init__(self, name: str, server: str | None = None) -> None:
        self.name = name
        self.server_kind = server or SYPHON_SERVER
        self._server = None
        self._enabled = True
        self._failed = False
        self.frames = 0
        # (texture, width, height) whose edge pixel has been logged.
        self._probed = None
        # Called with (sender name, reason) when the Metal path gives up.
        # The window sets it, to tell the user transparency is gone.
        self.on_fallback = None

    @property
    def active(self) -> bool:
        return self._server is not None

    def set_enabled(self, enabled: bool) -> None:
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self.release()

    def _start(self, flipped: bool):
        turn = "flipped" if flipped else "not flipped"
        if self.server_kind == "metal":
            try:
                path = _MetalPath(self.name)
            except Exception as exc:
                self._fall_back(exc)
            else:
                print(f"[chuutube] Syphon '{self.name}': Metal server, "
                      f"{turn} (SYPHON_FLIP)", file=sys.stderr)
                return path
        print(f"[chuutube] Syphon '{self.name}': OpenGL server, {turn} "
              "(SYPHON_FLIP)", file=sys.stderr)
        return _OpenGLPath(self.name)

    def _fall_back(self, exc) -> None:
        print(f"[chuutube] Syphon '{self.name}': the Metal path failed "
              f"({exc!r}) - using the OpenGL server. The 4:3 bars may arrive "
              "black.", file=sys.stderr)
        self.server_kind = "opengl"
        if self.on_fallback is not None:
            try:
                self.on_fallback(self.name, str(exc) or repr(exc))
            except Exception as err:
                print(f"[chuutube] Syphon fallback notice failed: {err}",
                      file=sys.stderr)

    def send(self, texture_id: int, width: int, height: int,
             flipped: bool) -> bool:
        """Publish one frame of an RGBA GL_TEXTURE_2D.

        `flipped` says the texture's row 0 is the TOP of the picture. Ours
        is (mpv renders top-down, which is what Spout wants). Hence
        SYPHON_FLIP, default on.

        A failure is reported once, not per frame: this runs up to 60 times
        a second, and a log full of one line hides everything else.
        """
        if not (SYPHON_AVAILABLE and self._enabled) or texture_id <= 0:
            return False
        if width <= 0 or height <= 0:
            return False
        if self._probed != (texture_id, width, height):
            self._probed = (texture_id, width, height)
            self._log_edge(texture_id, width, height)
        _reset_draw_state()
        try:
            if self._server is None:
                self._server = self._start(bool(flipped))
            self._server.publish(texture_id, int(width), int(height),
                                 bool(flipped))
        except Exception as exc:
            if isinstance(self._server, _MetalPath):
                self._fall_back(exc)
                self.release()
                return False
            if not self._failed:
                print(f"[chuutube] Syphon server '{self.name}' failed: {exc}",
                      file=sys.stderr)
                self._failed = True
            return False
        self.frames += 1
        return True

    def _log_edge(self, texture_id: int, width: int, height: int) -> None:
        """Log the left-edge pixel of what is about to be sent. Once per size.

        K, 1.0.4: a 4:3 clip reached Resolume with black bars on the Mac. This
        says whether the bars are still transparent when they leave this app;
        `--syphon-check` says whether Syphon carries them. A 16:9 clip has
        picture at the edge, so alpha 255 there is normal.
        """
        try:
            from OpenGL import GL
            read = GL.glGetIntegerv(GL.GL_READ_FRAMEBUFFER_BINDING)
            fbo = GL.glGenFramebuffers(1)
            try:
                GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, fbo)
                GL.glFramebufferTexture2D(GL.GL_READ_FRAMEBUFFER,
                                          GL.GL_COLOR_ATTACHMENT0,
                                          GL.GL_TEXTURE_2D, texture_id, 0)
                pixel = tuple(bytes(GL.glReadPixels(
                    1, height // 2, 1, 1, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE))[:4])
            finally:
                GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, int(read))
                GL.glDeleteFramebuffers(1, [fbo])
            if len(pixel) < 4:
                raise ValueError(f"read {len(pixel)} bytes, not 4")
        except Exception as exc:
            print(f"[chuutube] Syphon '{self.name}': could not read the edge "
                  f"pixel ({exc})", file=sys.stderr)
            return
        state = ("transparent" if pixel[3] == 0
                 else "opaque" if pixel[3] == 255 else "partly transparent")
        print(f"[chuutube] Syphon '{self.name}' {width}x{height}: left edge "
              f"RGBA {pixel} - {state}", file=sys.stderr)

    def release(self) -> None:
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:
                pass
            self._server = None


class SyphonOutput:
    """The app's two servers, one per player. Same interface as SpoutOutput."""

    kind = "Syphon"

    def __init__(self, name_a: str = SENDER_A, name_b: str = SENDER_B) -> None:
        self.players = [Sender(name_a), Sender(name_b)]

    @property
    def available(self) -> bool:
        return SYPHON_AVAILABLE

    @property
    def unavailable_reason(self) -> str:
        return SYPHON_ERROR

    def publish(self, targets, invert: bool, changed=None) -> None:
        """Send the targets that changed.

        `invert` is the Spout setting (SPOUT_INVERT) and is ignored here:
        Syphon's own orientation is SYPHON_FLIP, so changing one can never
        turn the other upside down.
        """
        for index, (sender, target) in enumerate(zip(self.players, targets)):
            if changed is not None and not changed[index]:
                continue
            if target is not None and target.valid:
                sender.send(target.tex, target.w, target.h, SYPHON_FLIP)

    def names(self) -> list[str]:
        return [s.name for s in self.players]

    def set_fallback_handler(self, handler) -> None:
        """handler(sender name, reason): a sender left Metal for OpenGL."""
        for s in self.players:
            s.on_fallback = handler

    def shutdown(self) -> None:
        for s in self.players:
            s.release()
