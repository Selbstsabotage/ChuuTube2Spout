# SPDX-License-Identifier: GPL-3.0-or-later
"""One video player: an mpv instance rendering into a private OpenGL texture.

The pixels never touch Python. libmpv decodes (on the GPU where possible) and
renders straight into an FBO we own; that FBO's colour attachment is the same
texture we hand to Spout. Python only shuffles property values around.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from collections import deque

from dataclasses import replace

from OpenGL import GL
from PySide6.QtCore import QObject, Signal

import mpv

from .config import (
    CACHE_DIR, FETCH_AUDIO, HWDEC, LOOP, MAX_TEXTURE_DIM, MODE,
    MPV_BLOCK_FOR_TARGET_TIME, MPV_FLIP_Y,
    SENDER_BACKGROUND, SENDER_FIT, background_rgba, canvas, canvas_for,
    empty_rgba,
    fit_report,
    PREFETCH,
    MPV_LOGLEVEL, PARTIAL_PLAY, PREVIEW_FLIP, SPOUT_INVERT, USE_PROXY,
)
from .downloader import (DownloadCancelled, DownloadError, LocalClip,
                         download)
from .headerproxy import get_proxy
from .quality import DEFAULT_HEIGHT
from .resolver import (ResolveError, Stream, UnsafeUrl, check_url, is_direct,
                       resolve)

# Re-exported so callers keep importing orientation from one place.
__all__ = ["Player", "RenderTarget", "MPV_FLIP_Y", "SPOUT_INVERT", "PREVIEW_FLIP"]

# A browser-ish default for the rare stream that carries no headers of its own.
# How long a download thread waits for mpv to let go before pressing on. It is
# a guard against a wedged GUI thread, not an expected delay.
RELEASE_TIMEOUT = 2.0

# The number in a downloader progress message ("downloading 42% of 120MiB").
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")

FALLBACK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class RenderTarget:
    """An FBO + RGBA8 texture pair sized to the Spout canvas.

    Sized to the VIDEO until 0.9.9.7, which is why every clip arrived in
    Resolume as a differently sized source. mpv scales into whatever target it
    is given, so the fix is simply to give it a constant one - see
    config.canvas_for.
    """

    def __init__(self) -> None:
        self.fbo: int = 0
        self.tex: int = 0
        self.w: int = 0
        self.h: int = 0

    @property
    def valid(self) -> bool:
        return self.fbo != 0 and self.w > 0 and self.h > 0

    def ensure(self, w: int, h: int) -> bool:
        """(Re)allocate for a w*h video. Requires a current GL context."""
        w = max(0, min(int(w), MAX_TEXTURE_DIM))
        h = max(0, min(int(h), MAX_TEXTURE_DIM))
        if w <= 0 or h <= 0:
            return False
        if self.fbo and (w, h) == (self.w, self.h):
            return True

        self.destroy()

        self.tex = int(GL.glGenTextures(1))
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0,
                        GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

        self.fbo = int(GL.glGenFramebuffers(1))
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                  GL.GL_TEXTURE_2D, self.tex, 0)
        status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
        # Start at the background colour so a freshly allocated target never
        # shows garbage - and so a transparent canvas starts transparent
        # rather than with one opaque frame.
        GL.glClearColor(*background_rgba())
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)

        if status != GL.GL_FRAMEBUFFER_COMPLETE:
            self.destroy()
            return False

        self.w, self.h = w, h
        return True

    def destroy(self) -> None:
        if self.fbo:
            try:
                GL.glDeleteFramebuffers(1, [self.fbo])
            except Exception:
                pass
            self.fbo = 0
        if self.tex:
            try:
                GL.glDeleteTextures(1, [self.tex])
            except Exception:
                pass
            self.tex = 0
        self.w = self.h = 0


def mpv_quote(value: str) -> str:
    """%LENGTH%VALUE, mpv's escape for list items containing separators.

    mpv parses list-valued options and properties as comma-separated, and the
    things we put in them - header values, signed URLs - are full of commas.
    Declaring the byte length removes the ambiguity entirely.
    """
    return f"%{len(value.encode('utf-8'))}%{value}"


# mpv's built-in Lua scripts, each behind its own load-* option. This app uses
# none of them: it renders into an FBO with the OSD off, no input bindings and
# no terminal, so the stats overlay, console, select menu, context menu and the
# rest have nothing to draw on and nobody to talk to. osc and ytdl are already
# off by name below.
#
# They are switched off rather than left idle because they were not idle. On a
# tester's machine they raised and caught Lua errors continuously - harmless in
# itself, but LuaJIT does that with Windows SEH exceptions, and faulthandler
# reported every one as a fatal exception and (until 0.9.9.12) dumped every
# Python thread while they ran. See logfile._enable_faulthandler.
#
# Names have been added across mpv versions, and python-mpv sets constructor
# options before mpv_initialize inside a try/finally - so an unknown name
# raises out of MPV() after the core has already been initialised. Hence the
# probe, rather than passing all of them and hoping.
BUILTIN_SCRIPT_OPTIONS = (
    "load-stats-overlay",
    "load-osd-console",
    "load-auto-profiles",
    "load-select",
    "load-positioning",
    "load-commands",
    "load-context-menu",
)

_script_options: dict | None = None


def builtin_scripts_off() -> dict:
    """{option: "no"} for each built-in script option this libmpv knows.

    Asked of a throwaway handle that is created and destroyed without ever
    being initialised: setting an option on an uninitialised handle validates
    the name immediately, and nothing is loaded or started until
    mpv_initialize, which this never calls. Done once per process.
    """
    global _script_options
    if _script_options is not None:
        return dict(_script_options)
    known: dict = {}
    create = getattr(mpv, "_mpv_create", None)
    set_option = getattr(mpv, "_mpv_set_option_string", None)
    destroy = getattr(mpv, "_mpv_terminate_destroy", None)
    if create and set_option and destroy:
        handle = None
        try:
            handle = create()
            for name in BUILTIN_SCRIPT_OPTIONS:
                try:
                    set_option(handle, name.encode("utf-8"), b"no")
                    known[name] = "no"
                except Exception:
                    pass          # not in this build - nothing to switch off
        except Exception as exc:
            print(f"[chuutube] could not check mpv's script options ({exc}) "
                  "- its built-in scripts stay on", file=sys.stderr)
        finally:
            if handle:
                try:
                    destroy(handle)
                except Exception:
                    pass
    _script_options = known
    if known:
        names = ", ".join(n.replace("load-", "") for n in known)
        print(f"[chuutube] mpv built-in scripts off: {names}", file=sys.stderr)
    return dict(known)


def mpv_list(items) -> str:
    return ",".join(mpv_quote(str(i)) for i in items)


def _make_proc_address_fn():
    """GL function loader for libmpv, backed by Qt's context.

    Kept as a module-level factory so the returned CFUNCTYPE object can be
    stashed on the Player - if it gets garbage collected while mpv still holds
    the pointer, the process dies in a way that is deeply unfun to debug.
    """
    from PySide6.QtGui import QOpenGLContext

    def _get_proc_address(_ctx, name):
        glctx = QOpenGLContext.currentContext()
        if glctx is None:
            return 0
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        try:
            addr = glctx.getProcAddress(name)
        except TypeError:
            addr = glctx.getProcAddress(name.encode("utf-8"))
        if addr is None:
            return 0
        try:
            return int(addr)
        except (TypeError, ValueError):
            # Some bindings hand back a voidptr-ish object instead of an int.
            return ctypes.cast(int(addr), ctypes.c_void_p).value or 0

    prototype = getattr(mpv, "MpvGlGetProcAddressFn", None)
    if prototype is None:  # older python-mpv without the public alias
        prototype = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_char_p)
    return prototype(_get_proc_address)


class PrefetchJob:
    """One background fetch of a URL nobody has asked to play yet.

    The whole point of prefetching is that pressing Load costs nothing, so a
    job has to be joinable: if the user loads the very URL being prefetched,
    load waits for this instead of starting a second yt-dlp on the same output
    path. --no-part means both would be writing into the same file.
    """

    def __init__(self, url: str, height: int | None) -> None:
        self.url = url
        self.height = height
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.clip: LocalClip | None = None
        self.error: BaseException | None = None
        self.started = time.monotonic()
        # The Load that joined this, if one has. Until then the half-written
        # file is offered to nobody and the downloader keeps asking.
        self._lock = threading.Lock()
        self._on_partial = None
        self._on_partial_lost = None
        self._on_progress = None
        # Told every progress line whether or not a Load has joined, so the
        # slot can show how far a background fetch has got.
        self.background = None

    def matches(self, url: str, height: int | None) -> bool:
        return self.url == url and self.height == height

    def attach(self, on_partial, on_partial_lost, progress=None) -> None:
        """A Load has joined this fetch: give IT the half-written file.

        Joining used to mean waiting for the last byte. Pasting a URL starts a
        prefetch 0.9s later, so pressing Load at any ordinary human speed joined
        one - and partial playback, which only the load path wired up, silently
        never happened. The downloader asks this job on every progress line;
        from here on the answer is the load's own callbacks.
        """
        with self._lock:
            self._on_partial = on_partial
            self._on_partial_lost = on_partial_lost
            self._on_progress = progress

    def progress(self, message: str) -> None:
        """The downloader's progress hook. Silent until a Load has joined.

        Without it a joined load said "already fetching this one..." for the
        whole wait, with no idea how far along it was, and the half-written
        file played with no percentage beside it.
        """
        with self._lock:
            hook = self._on_progress
        background = self.background
        if background is not None:
            background(message)
        if hook is not None:
            hook(message)

    def partial(self, path: str) -> bool:
        """The downloader's hook. False means nobody is watching yet."""
        with self._lock:
            hook = self._on_partial
        if hook is None:
            return False
        hook(path)
        return True

    def partial_lost(self) -> None:
        """The attempt failed after hand-over; the joined load must let go."""
        with self._lock:
            hook = self._on_partial_lost
        if hook is not None:
            hook()

    def result(self, timeout: float | None = None) -> LocalClip:
        """Block until it lands. Raises whatever the fetch raised."""
        if not self.done.wait(timeout):
            raise DownloadError("timed out waiting for the prefetch")
        if self.error is not None:
            raise self.error
        if self.clip is None:
            raise DownloadError("the prefetch produced no file")
        return self.clip


def _describe(stream: Stream) -> str:
    """What the status line says about a clip that is now playing.

    The title is the only part a VJ recognises at a glance - "1080p avc1" is
    true of everything in the set. It has been carried on the clip all along
    and never shown.
    """
    title = (stream.title or "").strip()
    return f"{stream.label} - {title}" if title else stream.label


def local_stream(clip: LocalClip) -> Stream:
    """A Stream describing a clip that is already on disk."""
    return Stream(video_url=clip.video_path, audio_url=clip.audio_path,
                  width=clip.width, height=clip.height, title=clip.title,
                  client="local", direct=True)


class Player(QObject):
    """A single mpv player. Owns its render target; does not own a GL context."""

    timeChanged = Signal(float)
    durationChanged = Signal(float)
    pauseChanged = Signal(bool)
    muteChanged = Signal(bool)
    sizeChanged = Signal(int, int)
    statusChanged = Signal(str)
    # True while a load is being resolved or downloaded, False once it has
    # landed or failed. Without it the Load button stayed live and a second
    # press started a second full download of the same clip.
    loadingChanged = Signal(bool)
    frameReady = Signal()
    # Emitted from the resolver thread; delivered on the GUI thread.
    _resolved = Signal(int, object)
    _failed = Signal(int, str)
    _progress = Signal(int, str)
    # Hybrid mode: the background download has landed and the network stream
    # can be swapped for the file on disk.
    _cached = Signal(int, object)
    # Enough of the file exists to start playing it while yt-dlp writes the rest.
    _partial = Signal(int, str)
    # "", "preparing" or "ready" for the URL currently in the box.
    prefetchChanged = Signal(str)
    _prefetch_state = Signal(str)
    # 0-100 while a background fetch downloads, for the Load button. Before
    # 0.9.9.19 the percentage existed but went nowhere until a Load joined, so
    # a clip caching behind one that was playing showed no sign of it at all.
    prefetchProgress = Signal(int)
    _prefetch_progress = Signal(int)
    # 0-100 for a load started with the Load button (foreground), so the
    # button can say "Loading 42%". Emitted on the GUI thread.
    loadProgress = Signal(int)
    # That attempt failed; stop reading its half-written file. Carries an Event
    # the download thread waits on, because the next attempt may overwrite the
    # very file mpv still has open.
    _partial_lost = Signal(int, object)

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.url: str = ""
        self.target = RenderTarget()
        self._render_ctx = None
        self._proc_addr_fn = None
        self._quality: int | None = DEFAULT_HEIGHT
        self._vid_w = 0
        self._vid_h = 0
        # Where mpv put the picture in our target, from its osd-dimensions:
        # (width, height, left, right, top, bottom). Used to clear the bars
        # ourselves - see _clear_bars.
        self._osd = None
        # The status line in two parts - see _set_status.
        self._headline = "idle"
        self._fetch_note = ""
        self._size_note = ""
        self._destroyed = False
        self._blank = False
        # True while our FBO holds the empty colour rather than a video frame.
        # The preview draws its checkerboard for these; asking mpv instead
        # would be asking about what it has been told, not what was drawn.
        self.empty = True
        # A window, not a single line - see _on_mpv_log for why one is not
        # enough.
        self._recent_log: deque[str] = deque(maxlen=4)
        self._repeats = 0
        # ffmpeg's lines get their own window and count - see _on_mpv_log.
        self._recent_ffmpeg: deque[str] = deque(maxlen=4)
        self._ffmpeg_repeats = 0
        self._load_token = 0
        self._pending_start: float | None = None
        # Which load is playing a half-written file, and which file that is.
        # The path matters because --no-part means yt-dlp writes straight into
        # it, so the finished download is that same file rather than a new one.
        self._partial_token: int | None = None
        self._partial_path: str | None = None
        # The one background fetch this slot is allowed to have running, and
        # the lock that keeps starting/replacing it atomic against load().
        self._job: PrefetchJob | None = None
        # (url, height) of a load that is downloading right now. The
        # prefetch debounce has to know about it - see prefetch().
        self._loading_key: tuple | None = None
        # The last position mpv actually reported. time-pos goes
        # UNAVAILABLE at EOF and around a load, and _get() then hands back
        # its default - which is how a handover 45 seconds into a clip
        # concluded the playhead was zero.
        self._last_time = 0.0
        self._job_lock = threading.Lock()
        self.stream: Stream | None = None
        self._proxy_tokens: list[str] = []

        opts = dict(
            vo="libmpv",
            hwdec=HWDEC,
            keep_open="yes",
            loop_file="inf" if LOOP else "no",  # VJ default: no dead air
            mute="yes",                # everything starts silent
            volume=100,
            idle="yes",
            osc=False,
            osd_level=0,
            terminal=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            audio_display="no",
            # We resolve URLs ourselves (see resolver.py), so mpv's own
            # ytdl_hook must stay out of the way - otherwise it would try to
            # re-interpret the already-resolved media URL we hand it.
            ytdl=False,
            cache="yes",
            demuxer_max_bytes="200MiB",
            demuxer_readahead_secs=20,
            # How a clip that is not the canvas shape is placed in it. Both are
            # mpv defaults, but they are load-bearing now that the target is a
            # fixed size rather than the video's own - so they are set here
            # rather than assumed.
            #
            # Without a fixed canvas the target is the clip's own shape, and
            # SENDER_MAX_* scaling can leave it a pixel off after rounding;
            # keeping the aspect there would draw a 1 px bar along one edge.
            # Filling the target exactly is the right answer in that case.
            keepaspect=(SENDER_FIT != "stretch") if canvas()[0] else False,
            panscan=(1.0 if SENDER_FIT == "fill" and canvas()[0] else 0.0),
        )
        # Before mpv_initialize, which is the only point at which these count:
        # the scripts are loaded during initialisation.
        for name, value in builtin_scripts_off().items():
            opts[name.replace("-", "_")] = value
        self.mpv = mpv.MPV(log_handler=self._on_mpv_log,
                           loglevel=MPV_LOGLEVEL, **opts)

        self._apply_background()

        self.mpv.observe_property("time-pos", self._on_time_pos)
        self.mpv.observe_property("duration", self._on_duration)
        self.mpv.observe_property("pause", self._on_pause)
        self.mpv.observe_property("mute", self._on_mute)
        self.mpv.observe_property("dwidth", self._on_dwidth)
        self.mpv.observe_property("dheight", self._on_dheight)
        self.mpv.observe_property("osd-dimensions", self._on_osd_dimensions)

        self._resolved.connect(self._on_resolved)
        self._cached.connect(self._on_cached)
        self._partial.connect(self._on_partial)
        self._partial_lost.connect(self._on_partial_lost)
        self._prefetch_state.connect(self.prefetchChanged)
        self._prefetch_progress.connect(self.prefetchProgress)
        self._failed.connect(self._on_failed)
        self._progress.connect(self._on_progress)

    # -- GL lifecycle ---------------------------------------------------------

    def attach_gl(self) -> None:
        """Create the mpv render context. Call with the GL context current."""
        if self._render_ctx is not None:
            return
        self._proc_addr_fn = _make_proc_address_fn()
        self._render_ctx = mpv.MpvRenderContext(
            self.mpv, "opengl",
            opengl_init_params={"get_proc_address": self._proc_addr_fn},
        )
        self._render_ctx.update_cb = self._on_mpv_update

    def _on_mpv_update(self) -> None:
        # Called from an mpv thread. Signal emission is thread-safe; the
        # connected slot runs queued on the GUI thread.
        if not self._destroyed:
            self.frameReady.emit()

    def render(self) -> bool:
        """Draw a pending frame into our FBO. Returns True if we drew.

        Requires a current GL context.
        """
        ctx = self._render_ctx
        if ctx is None:
            return False

        if self._blank:
            # Clearing an unloaded slot has to happen here, with the context
            # current. mpv stops producing frames the moment it is told to stop,
            # so without this the FBO - and therefore both the preview and the
            # Spout sender - keep showing the last frame of a clip that is no
            # longer loaded. Returning True publishes the empty frame:
            # transparent, or black with EMPTY_TRANSPARENT=0.
            self._blank = False
            self.empty = True
            if self.target.valid:
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.target.fbo)
                GL.glDisable(GL.GL_SCISSOR_TEST)
                GL.glViewport(0, 0, self.target.w, self.target.h)
                GL.glClearColor(*empty_rgba())
                GL.glClear(GL.GL_COLOR_BUFFER_BIT)
                GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
                return True

        if not ctx.update():
            return False
        if self._vid_w <= 0 or self._vid_h <= 0:
            # Still no frame. The target stays unallocated, which is what lets
            # the idle black frame hold the sender open in the meantime.
            return False
        w, h = canvas_for(self._vid_w, self._vid_h)
        if not self.target.ensure(w, h):
            return False
        ctx.render(
            flip_y=MPV_FLIP_Y,
            opengl_fbo={"w": self.target.w, "h": self.target.h, "fbo": self.target.fbo},
            # Draw the frame that is current and come straight back. Left at
            # libmpv's default this call SLEEPS until the frame's display time,
            # which is correct for a player pacing itself to a monitor and
            # wrong for us - it parks the GUI thread inside mpv for most of
            # every frame interval, and makes two slots pace each other. See
            # config.MPV_BLOCK_FOR_TARGET_TIME.
            block_for_target_time=MPV_BLOCK_FOR_TARGET_TIME,
        )
        if canvas()[0] and SENDER_BACKGROUND != "black":
            self._clear_bars()
        self.empty = False
        return True

    def detach_gl(self) -> None:
        """Tear down GL-side resources. Call with the GL context current."""
        if self._render_ctx is not None:
            try:
                self._render_ctx.update_cb = None
            except Exception:
                pass
            try:
                self._render_ctx.free()
            except Exception:
                pass
            self._render_ctx = None
        self.target.destroy()

    def shutdown(self) -> None:
        self._destroyed = True
        try:
            self.mpv.terminate()
        except Exception:
            pass

    # -- transport ------------------------------------------------------------

    # -- prefetch -------------------------------------------------------------

    def prefetch(self, url: str) -> None:
        """Start fetching `url` before anyone asks to play it.

        Everything expensive about a load happens before a byte of video moves:
        page extraction, a PO token, a JS challenge - about five seconds that
        does not depend on the decision to play. Doing it while the current clip
        is still running is free, and makes Load instant.

        Only for the download path. In stream mode there is nothing to warm, and
        in hybrid the resolve is the fast half anyway.
        """
        if not PREFETCH or MODE != "download":
            return
        try:
            url = check_url(url)
        except UnsafeUrl:
            return                      # load() will say why; do not shout twice
        if is_direct(url):
            return                      # already a media URL; nothing to fetch
        height = self._quality
        with self._job_lock:
            if self._loading_key == (url, height):
                # A load is already downloading this. Starting a prefetch too
                # means two yt-dlp processes writing one output path, because
                # --no-part gives them the same filename - the exact collision
                # _claim_prefetch() exists to prevent, arriving from the other
                # direction. It happens when Load is pressed inside the paste
                # debounce: the load starts first, finds no job to claim, and
                # the timer then fires into the gap. Seen in the wild on a
                # 560 MB clip, fetched twice.
                return
            job = self._job
            if job is not None and job.matches(url, height) and not job.cancel.is_set():
                return                  # already doing exactly this
            if job is not None and not job.done.is_set():
                # The box changed. Let go of the old one rather than letting two
                # fetches share the link - on a venue connection that is the
                # difference between one clip ready and neither.
                job.cancel.set()
            job = self._job = PrefetchJob(url, height)
        job.background = lambda message, job=job: self._report_prefetch_progress(
            job, message)

        self._prefetch_state.emit("preparing")

        def work(job=job):
            try:
                job.clip = download(
                    url, height, CACHE_DIR, cancel=job.cancel, ahead=True,
                    progress=job.progress,
                    on_partial=job.partial if PARTIAL_PLAY else None,
                    on_partial_lost=job.partial_lost if PARTIAL_PLAY else None)
            except BaseException as exc:      # noqa: BLE001 - re-raised in result()
                job.error = exc
            finally:
                job.done.set()
            with self._job_lock:
                current = self._job is job
            if not current:
                return                  # superseded; its state is not the news
            if isinstance(job.error, DownloadCancelled):
                return
            self._prefetch_state.emit("" if job.error is not None else "ready")

        threading.Thread(target=work, name=f"prefetch-{self.name}",
                         daemon=True).start()

    def _report_prefetch_progress(self, job: PrefetchJob, message: str) -> None:
        """Pass a background fetch's percentage on - if it is still the news.

        Runs on the download thread. A superseded job keeps printing until its
        yt-dlp notices the cancel, and its numbers must not land on the button
        that now belongs to a different URL.
        """
        match = _PERCENT.search(message)
        if match is None:
            return
        with self._job_lock:
            current = self._job is job
        if current:
            self._prefetch_progress.emit(min(100, int(float(match.group(1)))))

    def cancel_prefetch(self) -> None:
        """Abandon any background fetch. Called when the slot is cleared."""
        with self._job_lock:
            job, self._job = self._job, None
        if job is not None and not job.done.is_set():
            job.cancel.set()
        self._prefetch_state.emit("")

    def _claim_prefetch(self, url: str, height) -> PrefetchJob | None:
        """The in-flight job for exactly this URL, if there is one.

        load() waits on it instead of starting its own yt-dlp: --no-part means
        the second one would be writing into the file the first one is still
        filling, which is the same collision the partial-playback work exists to
        avoid.
        """
        with self._job_lock:
            job = self._job
            if job is not None and job.matches(url, height) and not job.cancel.is_set():
                return job
        return None

    def load(self, url: str, start_at: float | None = None) -> None:
        """Resolve `url` off the GUI thread, then hand mpv a real media URL."""
        try:
            url = check_url(url)
        except UnsafeUrl as exc:
            # Refused at the boundary, before any subprocess exists to be
            # tricked. The status line says why rather than failing silently.
            self._set_status(str(exc))
            # The panel greys Load out optimistically on click; nothing else
            # will report this attempt as over.
            self.loadingChanged.emit(False)
            return
        self.url = url
        self._pending_start = start_at
        self._last_time = 0.0          # a new clip starts at the start
        self._partial_token = None
        self._partial_path = None
        self._load_token += 1
        token = self._load_token
        height = self._quality
        self._size_note = ""               # belongs to the clip being replaced
        self._set_status("resolving...")
        self.loadingChanged.emit(True)
        with self._job_lock:
            # Claimed before the thread starts, so a debounce firing a moment
            # from now already sees it.
            self._loading_key = (url, height)

        def say(msg):
            self._progress.emit(token, msg)

        def work():
            try:
                if is_direct(url) or MODE == "stream":
                    self._resolved.emit(token, resolve(url, height, progress=say))
                    return
                if MODE == "hybrid":
                    self._hybrid(token, url, height, say)
                    return
                def early(path, token=token):
                    self._partial.emit(token, path)

                def gave_up(token=token):
                    # Blocks this thread until the GUI thread confirms mpv has
                    # released the file - the whole point is to not still be
                    # reading it when the next attempt starts writing.
                    released = threading.Event()
                    self._partial_lost.emit(token, released)
                    if not released.wait(RELEASE_TIMEOUT):
                        print("[chuutube] timed out waiting for mpv to release "
                              "the partial file", file=sys.stderr)

                warm = self._claim_prefetch(url, height)
                if warm is not None:
                    # This exact fetch is already running. Wait for it rather
                    # than starting a second yt-dlp on the same output path -
                    # with --no-part they would be writing the same file.
                    say("already fetching this one...")
                    # Said outright. Without it, "was Load pressed during the
                    # prefetch?" had to be inferred from lines that were
                    # missing - no cache hit, "prefetched" rather than "ready".
                    print(f"[chuutube] Load joined the background download "
                          f"({time.monotonic() - warm.started:.1f}s after it "
                          f"started)", file=sys.stderr)
                    warm.attach(early if PARTIAL_PLAY else None,
                                gave_up if PARTIAL_PLAY else None, say)
                    clip = warm.result()
                else:
                    clip = download(url, height, CACHE_DIR, progress=say,
                                    on_partial=early if PARTIAL_PLAY else None,
                                    on_partial_lost=gave_up if PARTIAL_PLAY else None)
                if self._partial_token == token:
                    # Already playing the half-written file: reload the finished
                    # one at the same instant rather than starting it over.
                    self._cached.emit(token, local_stream(clip))
                else:
                    self._resolved.emit(token, local_stream(clip))
            except DownloadCancelled:
                # Only reachable via a claimed prefetch that was superseded.
                # The newer load is the one that matters; say nothing.
                pass
            except (ResolveError, DownloadError) as exc:
                self._failed.emit(token, str(exc))
            except Exception as exc:  # noqa: BLE001 - surface anything at all
                self._failed.emit(token, f"{exc.__class__.__name__}: {exc}")
            finally:
                with self._job_lock:
                    if self._loading_key == (url, height):
                        self._loading_key = None

        threading.Thread(target=work, name=f"resolve-{self.name}",
                         daemon=True).start()

    def _hybrid(self, token: int, url: str, height, say) -> None:
        """Show something now, cache underneath, then swap to the file.

        Runs on the resolve thread. The two halves are deliberately independent:
        a resolve that fails still gets the download, and a download that fails
        leaves whatever is already playing alone. Only the case where both fail
        is a load failure.
        """
        streaming = False
        try:
            remote = resolve(url, height, progress=say)
        except (ResolveError, DownloadError) as exc:
            say(f"cannot stream ({exc}) - downloading instead")
        else:
            streaming = True
            self._resolved.emit(token, remote)

        try:
            clip = download(url, height, CACHE_DIR, progress=say)
        except (ResolveError, DownloadError) as exc:
            if streaming:
                # It is playing. Losing the cached copy costs seeking and
                # looping, not the clip.
                self._progress.emit(token, f"streaming only - no cache: {exc}")
            else:
                self._failed.emit(token, str(exc))
            return

        if streaming:
            self._cached.emit(token, local_stream(clip))
        else:
            self._resolved.emit(token, local_stream(clip))

    def _stale(self, token: int) -> bool:
        """True if a newer load has started since this one was queued."""
        return token != self._load_token or self._destroyed

    def _set_status(self, headline: str) -> None:
        """Say what the slot is doing. Notes are kept and appended to it.

        The status line used to be whatever was written to it last, and three
        things wrote to it: the load ("1080p - Title", or "playing while it
        downloads"), the download's progress, and mpv reporting the video size.
        The size lands about 100ms after every load - so every title was
        replaced by "1920x1080" almost as soon as it appeared, and "playing
        while it downloads" survived for a split second. Now the headline says
        what is happening and the other two are notes appended to it.
        """
        self._headline = headline
        self._fetch_note = ""
        self._show_status()

    def _show_status(self) -> None:
        parts = [getattr(self, "_headline", ""),
                 getattr(self, "_fetch_note", ""),
                 getattr(self, "_size_note", "")]
        self.statusChanged.emit(" \u00b7 ".join(p for p in parts if p))

    def _on_progress(self, token: int, message: str) -> None:
        if self._stale(token):
            return
        match = _PERCENT.search(message)
        if match is not None:
            self.loadProgress.emit(min(100, int(float(match.group(1)))))
        if self._partial_token == token:
            # Already playing the half-written file: the progress is a note on
            # that, not a replacement for it.
            self._fetch_note = message.removeprefix("downloading ")
            self._show_status()
        else:
            self._set_status(message)

    def _on_failed(self, token: int, message: str) -> None:
        if self._stale(token):
            return
        self.stream = None
        self._set_status(message)
        self.loadingChanged.emit(False)

    def _on_resolved(self, token: int, stream: Stream) -> None:
        if self._stale(token):
            return
        self.stream = stream
        video_url, audio_url = self._prepare_urls(stream)
        self._apply_http(stream, audio_url)

        try:
            start = self._pending_start
            self.mpv["start"] = f"{start:.3f}" if start else "none"
        except Exception:
            pass
        self._pending_start = None

        self._set_status(_describe(stream))
        try:
            self.mpv.loadfile(video_url, "replace")
        except Exception as exc:
            self._set_status(f"mpv refused the URL: {exc}")
        self.loadingChanged.emit(False)

    def _on_partial(self, token: int, path: str) -> None:
        """Play the file yt-dlp is still writing into.

        No `start` and no seeking: the point is the first frame, and the rest
        arrives underneath. mpv reads a growing file happily, which is why
        _command() drops --part - Windows will not rename a file mpv has open,
        and that rename is the last thing yt-dlp would otherwise do.
        """
        if self._stale(token):
            return
        self._partial_token = token
        self._partial_path = path
        self.stream = Stream(video_url=path, direct=True, client="local")
        try:
            self.mpv["start"] = "none"
        except Exception:
            pass
        # A growing file ends where it ended when mpv opened it: the demuxer
        # stops at that byte and reports "partial file". With loop-file=inf
        # that is a jump back to zero - which from the room is indistinguishable
        # from "it restarted once it finished downloading", and is exactly what
        # was reported. Sit on the last frame instead. The handover re-opens the
        # finished file seconds later and resumes at the playhead, so the loop
        # comes back on with it.
        #
        # This only bites when the download is slower than playback. A fast
        # transfer finishes before mpv reaches the end and never shows it, which
        # is why it looked fixed.
        self._set("loop-file", "no")
        try:
            self.mpv.loadfile(path, "replace")
        except Exception as exc:
            # Not fatal: the download carries on and _on_cached still fires.
            self._set_status(f"waiting for the file: {exc}")
            self._partial_token = None
            self._partial_path = None
            return
        self._set_status("playing while it downloads")
        # There is a picture; the button has no reason to stay greyed out.
        self.loadingChanged.emit(False)

    def _on_partial_lost(self, token: int, released) -> None:
        """Let go of a half-written file whose download attempt failed.

        Goes black rather than holding the last frame: the slot is mid-load,
        and a frozen frame from a clip that is about to be rewritten underneath
        is a worse lie than an empty preview.

        Deliberately not stop(), which bumps _load_token and would orphan the
        very load still trying to succeed.
        """
        try:
            if self._stale(token) or self._partial_token != token:
                return
            self._partial_token = None
            self._partial_path = None
            self.stream = None
            try:
                self.mpv.command("stop")
            except Exception:
                pass
            self._blank = True
            self.frameReady.emit()
            self._set_status("that client failed - trying another")
        finally:
            # Always, even on the paths that returned early: the download
            # thread is blocked on this and a missed set() costs it a stall.
            released.set()

    def _on_cached(self, token: int, stream: Stream) -> None:
        """Swap the network stream for the finished file, at the same instant.

        mpv keeps `pause` across a loadfile, so a paused slot stays paused. The
        playhead does not survive, which is what `start` is for.

        The reload is not optional, even when --no-part means the finished file
        is byte-for-byte the file mpv already has open. mpv caches the size at
        open and does not pick the growth up: skipping the reload produced

            error ffmpeg/demuxer: mov,mp4: stream 0, offset 0x802800: partial file
            info cplayer: Video: no video

        - mpv running off the end of what it thought the file was, roughly a
        third of the way in, and loop_file sending it back to zero. Re-opening
        the file is the only thing that tells mpv how long it really is.
        """
        if self._stale(token):
            return
        live = self.time_pos
        # A partial file sits at EOF most of the time - it ends wherever the
        # download had reached - and mpv answers "unavailable" for time-pos
        # there. _get() turns that into 0.0, so reading it at this one instant
        # was asking the player the one question it cannot answer. What
        # playback actually reached is remembered as it happens instead.
        at = live if live > 0.05 else self._last_time
        was_partial = self._partial_path is not None
        self._partial_token = None
        self._partial_path = None
        self.stream = stream
        video_url, audio_url = self._prepare_urls(stream)
        self._apply_http(stream, audio_url)
        try:
            # No "barely started, just begin again" threshold. The handover
            # happens seconds after playback starts, so a threshold of any size
            # is exactly the case that hits - and starting over one second in
            # is what "it starts from the beginning" was the first time.
            self.mpv["start"] = f"{at:.3f}" if at > 0.05 else "none"
        except Exception:
            pass
        try:
            self.mpv.loadfile(video_url, "replace")
        except Exception as exc:
            # The stream is still playing; say so rather than looking broken.
            self._set_status(f"kept streaming - {exc}")
            return
        # Whatever the partial file turned off, the finished one gets back.
        self._set("loop-file", "inf" if LOOP else "no")
        if was_partial:
            # Says which of the two things happened, so the next report of
            # "it restarted" arrives with the answer already in it.
            if at > 0.05:
                print(f"[chuutube] handover: resumed at {at:.1f}s "
                      f"(live {live:.1f}s, remembered {self._last_time:.1f}s)",
                      file=sys.stderr)
            else:
                # Both empty means playback never really started, which would be
                # a different bug from the one this line was added for.
                print(f"[chuutube] handover: restarted from the beginning "
                      f"(live {live:.1f}s, remembered {self._last_time:.1f}s, "
                      f"paused={self.paused})", file=sys.stderr)
        # No " (cached)" suffix: the stream is a local file, and its label
        # already says "1080p cached". The suffix predates that label and
        # came from hybrid mode; together they read "1080p cached - Title
        # (cached)".
        self._set_status(_describe(stream))

    def _prepare_urls(self, stream: Stream) -> tuple[str, str | None]:
        """Swap remote URLs for loopback ones that carry the right headers."""
        if not FETCH_AUDIO:
            # One place to drop it, so a stream resolved for playback and a
            # clip fetched to disk behave the same way.
            stream = replace(stream, audio_url=None)
        if stream.direct or not stream.headers or not USE_PROXY:
            return stream.video_url, stream.audio_url

        proxy = get_proxy()
        old, self._proxy_tokens = self._proxy_tokens, []
        video = proxy.publish(stream.video_url, stream.headers)
        audio = (proxy.publish(stream.audio_url, stream.headers)
                 if stream.audio_url else None)
        for url in (video, audio):
            if url:
                self._proxy_tokens.append(url.rsplit("/", 1)[-1])
        proxy.forget(old)
        return video, audio

    def _apply_http(self, stream: Stream, audio_url: str | None) -> None:
        """Make mpv's request look like the one the URL was issued for.

        Belt and braces: the loopback relay already guarantees the headers
        arrive, but if it is disabled these options are the only thing standing
        between a signed URL and a 403 - on libmpv builds that still honour them.
        """
        user_agent = FALLBACK_UA
        fields = []
        for key, value in (stream.headers or {}).items():
            low = key.lower()
            if low == "user-agent":
                user_agent = value
            elif low in ("accept-encoding", "range", "host", "connection"):
                continue  # mpv/ffmpeg manage these themselves
            else:
                fields.append(f"{key}: {value}")

        self._set("user-agent", user_agent)
        self._set_list("http-header-fields", fields)
        self._set_list("audio-files", [audio_url] if audio_url else [])

    def _try_set(self, prop: str, value) -> bool:
        """Set a property, quietly, reporting only whether it took.

        For options mpv has renamed across versions: the caller offers the
        spellings in order and keeps the first that works. Printing each miss
        would make a successful startup look like four failures.
        """
        try:
            self.mpv[prop] = value
            return True
        except Exception:
            return False

    def _apply_background(self) -> None:
        """What mpv paints where the video does not reach.

        mpv has renamed this option more than once - `background` has taken a
        colour in some versions and none/color/tiles in others, with
        `background-color` alongside - so each spelling is tried rather than
        assumed. Falling through all of them leaves mpv's own default, which is
        opaque black: the right thing to end up with if the only casualty is
        somebody's custom colour.
        """
        from .config import background_colour
        if SENDER_BACKGROUND == "transparent":
            for prop, value in (("background", "none"), ("alpha", "yes")):
                if self._try_set(prop, value):
                    return
            print(f"[{self.name}] this mpv will not do a transparent "
                  "background - the bars stay black", file=sys.stderr)
            return
        colour = background_colour()
        for prop in ("background-color", "background"):
            if self._try_set(prop, colour):
                return
        if colour != "#000000":
            print(f"[{self.name}] this mpv will not take a background colour "
                  f"- the bars stay black, not {colour}", file=sys.stderr)

    def _set(self, prop: str, value) -> None:
        try:
            self.mpv[prop] = value
        except Exception as exc:
            print(f"[{self.name}] could not set {prop}: {exc}", file=sys.stderr)

    def _set_list(self, prop: str, items) -> None:
        """String-list properties take a node array; fall back to %n% quoting."""
        items = list(items)
        try:
            self.mpv[prop] = items
            return
        except Exception:
            pass
        try:
            self.mpv[prop] = mpv_list(items)
        except Exception as exc:
            print(f"[{self.name}] could not set {prop}: {exc}", file=sys.stderr)

    def reload_preserving_position(self) -> None:
        if not self.url:
            return
        pos = self.time_pos or 0.0
        was_paused = self.paused
        self.load(self.url, start_at=pos)
        self.paused = was_paused

    def stop(self) -> None:
        self.url = ""
        self.stream = None
        self.cancel_prefetch()
        self._load_token += 1     # orphan any resolve still in flight
        try:
            self.mpv.command("stop")
        except Exception:
            pass
        self._vid_w = self._vid_h = 0
        # An empty slot should look empty, here and in Resolume. The render
        # loop only wakes for a frame, so ask it for one.
        self._blank = True
        self.frameReady.emit()
        self._set_status("idle")
        # stop() orphans any resolve in flight, so nothing else will report the
        # load as finished. Without this the Load button stays greyed out after
        # clearing a slot mid-download.
        self.loadingChanged.emit(False)

    def seek(self, seconds: float, exact: bool = True) -> None:
        try:
            self.mpv.seek(seconds, "absolute", "exact" if exact else "keyframes")
        except Exception:
            pass

    def seek_relative(self, delta: float) -> None:
        try:
            self.mpv.seek(delta, "relative", "keyframes")
        except Exception:
            pass

    # -- properties -----------------------------------------------------------

    def _get(self, prop, default=None):
        try:
            value = self.mpv[prop]
        except Exception:
            return default
        return default if value is None else value

    @property
    def paused(self) -> bool:
        return bool(self._get("pause", True))

    @paused.setter
    def paused(self, value: bool) -> None:
        try:
            self.mpv.pause = bool(value)
        except Exception:
            pass

    def toggle_pause(self) -> None:
        self.paused = not self.paused

    @property
    def muted(self) -> bool:
        return bool(self._get("mute", True))

    @muted.setter
    def muted(self, value: bool) -> None:
        try:
            self.mpv.mute = bool(value)
        except Exception:
            pass

    @property
    def time_pos(self) -> float:
        return float(self._get("time-pos", 0.0) or 0.0)

    @property
    def duration(self) -> float:
        return float(self._get("duration", 0.0) or 0.0)

    @property
    def video_size(self) -> tuple[int, int]:
        return self._vid_w, self._vid_h

    @property
    def quality(self) -> int | None:
        return self._quality

    def set_quality(self, height: int | None) -> None:
        if height == self._quality:
            return
        self._quality = height
        if self.url:
            self.reload_preserving_position()

    def actual_height(self) -> int:
        return self._vid_h

    @property
    def resolved_via(self) -> str:
        return self.stream.client if self.stream else ""

    # -- logging --------------------------------------------------------------

    def _on_mpv_log(self, level: str, component: str, message: str) -> None:
        message = (message or "").strip()
        if not message:
            return

        # ffmpeg repeats some warnings once per frame - "Late SEI is not
        # implemented" arrives hundreds of times for one clip and buries
        # everything worth reading. Collapse repeats rather than filtering by
        # content, so nothing is dropped silently.
        #
        # A WINDOW, not "same as the previous line". ffmpeg emits that warning
        # as a pair - "... is not implemented", then "If you want to help,
        # upload a sample ..." - so the two alternate forever and a one-line
        # memory never matches either of them. That is how a flood survived the
        # first version of this, and why the test for it has to log a pair.
        #
        # Errors are never collapsed: they are rare, they carry the status
        # line, and swallowing a second one to save a line of console is a bad
        # trade.
        loud = level in ("error", "fatal")
        # ffmpeg has ONE log callback for the whole process, and libmpv hands it
        # to whichever mpv instance initialised first (av_log.c keeps a single
        # log_mpv_instance). So every "ffmpeg", "ffmpeg/demuxer", "ffmpeg/video"
        # line from EITHER slot arrives here, on slot A. Seen as slot B's half-
        # downloaded mp4 reporting "Invalid NAL unit size" and "partial file"
        # as [mpv:A] - while A was playing a webm - and, worse, as an error in
        # A's status line, about a problem A did not have.
        #
        # There is no way to tell which slot an ffmpeg line belongs to, so it
        # is labelled as belonging to neither and kept off both status lines.
        # mpv's own components (cplayer, vd, demux, vo...) are per instance and
        # still report where they belong. The repeat count is kept separately
        # too, or B's endless "Late SEI" pair was summed up at exit as
        # "[mpv:A] (previous 4 line(s) repeated 16 more time(s))".
        shared = component.startswith("ffmpeg")
        label = "ffmpeg" if shared else f"mpv:{self.name}"
        if shared:
            recent, repeats = self._recent_ffmpeg, self._ffmpeg_repeats
        else:
            recent, repeats = self._recent_log, self._repeats
        if not loud and message in recent:
            if shared:
                self._ffmpeg_repeats += 1
            else:
                self._repeats += 1
            return
        if repeats:
            print(f"[{label}] (previous {len(recent)} line(s) "
                  f"repeated {repeats} more time(s))", file=sys.stderr)
            if shared:
                self._ffmpeg_repeats = 0
            else:
                self._repeats = 0
            recent.clear()
        recent.append(message)

        print(f"[{label}] {level} {component}: {message}", file=sys.stderr)
        if level in ("error", "fatal") and not shared:
            # Surface the first line only - mpv errors can be paragraphs.
            self._set_status(message.splitlines()[0][:120])

    # -- observers (called on mpv threads) ------------------------------------

    def _on_time_pos(self, _name, value) -> None:
        if value is not None:
            self._last_time = float(value)
        self.timeChanged.emit(float(value or 0.0))

    def _on_duration(self, _name, value) -> None:
        # mpv clears this to None around loads, seeks and end-of-file. Passing
        # those on as 0.0 made the seek bar disable itself part-way through a
        # clip and never re-enable - reported as "the slider cannot be moved".
        if value is None:
            return
        self.durationChanged.emit(float(value))

    def _on_pause(self, _name, value) -> None:
        self.pauseChanged.emit(bool(value))

    def _on_mute(self, _name, value) -> None:
        self.muteChanged.emit(bool(value))

    def _warn_if_badly_cropped(self, out_w: int, out_h: int) -> None:
        """Say so when "fill" is about to throw most of the frame away.

        A vertical phone video on a 16:9 canvas keeps about a third of its
        width in fill mode. That is a legitimate thing to ask for and a
        catastrophic thing to discover on stage, so it is said out loud rather
        than left for someone to notice in the preview.
        """
        if SENDER_FIT != "fill" or out_w <= 0 or out_h <= 0:
            return
        _, _, kept = fit_report(self._vid_w, self._vid_h, out_w, out_h)
        if kept >= 0.6:
            return
        axis = ("width" if self._vid_w / self._vid_h > out_w / out_h
                else "height")
        print(f"[{self.name}] SENDER_FIT=fill is cropping "
              f"{(1 - kept) * 100:.0f}% of the {axis} off this clip "
              f"({self._vid_w}x{self._vid_h} into {out_w}x{out_h}). "
              "SENDER_FIT=fit keeps all of it, with bars.", file=sys.stderr)

    def _on_dwidth(self, _name, value) -> None:
        self._vid_w = int(value or 0)
        self._emit_size()

    def _on_dheight(self, _name, value) -> None:
        self._vid_h = int(value or 0)
        self._emit_size()

    def _on_osd_dimensions(self, _name, value) -> None:
        try:
            self._osd = (int(value["w"]), int(value["h"]), int(value["ml"]),
                         int(value["mr"]), int(value["mt"]), int(value["mb"]))
        except (TypeError, KeyError, ValueError):
            self._osd = None

    def picture_rect(self) -> tuple[int, int, int, int] | None:
        """Where the picture is in the target, (x0, y0, x1, y1) in GL terms.

        None when it fills the target (or nothing is known). mpv's own margins
        when they are for this target size - its rounding is the truth - and
        otherwise the same fit worked out here.
        """
        tw, th = self.target.w, self.target.h
        if tw <= 0 or th <= 0 or self._vid_w <= 0 or self._vid_h <= 0:
            return None
        osd = self._osd
        if osd and (osd[0], osd[1]) == (tw, th):
            left, right, top, bottom = osd[2:]
        else:
            vw, vh, _ = fit_report(self._vid_w, self._vid_h, tw, th)
            left = (tw - vw) // 2
            right = tw - vw - left
            top = (th - vh) // 2
            bottom = th - vh - top
        if max(left, right, top, bottom) <= 0:
            return None
        # Row 0 of the target is the top of the picture unless MPV_FLIP_Y.
        y0, y1 = (bottom, th - top) if MPV_FLIP_Y else (top, th - bottom)
        return left, y0, tw - right, y1

    def _clear_bars(self) -> None:
        """Paint the bars SENDER_BACKGROUND, whatever mpv did with them.

        K, 0.9.9.38: with a fixed canvas and SENDER_BACKGROUND=transparent the
        bars still reached Resolume black. Which mpv option makes its clear
        transparent has changed between mpv versions, and this build painted
        them opaque. So after mpv draws, the bars are cleared here to the
        colour asked for - transparent included - with two scissored clears.
        """
        rect = self.picture_rect()
        if rect is None:
            return
        tw, th = self.target.w, self.target.h
        x0, y0, x1, y1 = rect
        bars = [(0, 0, x0, th), (x1, 0, tw - x1, th),
                (x0, 0, x1 - x0, y0), (x0, y1, x1 - x0, th - y1)]
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.target.fbo)
        GL.glEnable(GL.GL_SCISSOR_TEST)
        GL.glClearColor(*background_rgba())
        for x, y, w, h in bars:
            if w > 0 and h > 0:
                GL.glScissor(x, y, w, h)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)

    def _emit_size(self) -> None:
        if self._vid_w > 0 and self._vid_h > 0:
            self.sizeChanged.emit(self._vid_w, self._vid_h)
            out_w, out_h = canvas_for(self._vid_w, self._vid_h)
            self._warn_if_badly_cropped(out_w, out_h)
            if (out_w, out_h) == (self._vid_w, self._vid_h):
                # Says nothing the headline's "1080p" does not already say,
                # and appending it pushed long titles further into the "...".
                self._size_note = ""
            else:
                # Both numbers, because "1920x1080" on a clip that is actually
                # 640x480 is the kind of half-truth that costs an hour when
                # someone is wondering why it looks soft.
                self._size_note = (f"{self._vid_w}x{self._vid_h} "
                                   f"in {out_w}x{out_h}")
            self._show_status()
