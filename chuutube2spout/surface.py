# SPDX-License-Identifier: GPL-3.0-or-later
"""The preview surface: one OpenGL context, two players, two Spout senders.

Rendering and Spout publishing happen on a timer with the context made current
explicitly - deliberately NOT inside paintGL. paintGL only runs when the widget
is visible, so tying the output to it would stall Spout the moment the window
was minimised behind Resolume. Here, the preview is a side effect of a pipeline
that keeps running regardless.
"""

from __future__ import annotations

import sys
import time

from OpenGL import GL
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from .config import (DRAG_PAUSE_RENDER, DRAG_SETTLE_MS, DRAG_TICK_MS,
                     IDLE_SENDER_HEIGHT, IDLE_SENDER_MS, IDLE_SENDER_WIDTH,
                     IDLE_SENDERS, PERF_LOG_S, PREVIEW_ASPECT, PREVIEW_FPS,
                     SENDER_BACKGROUND, canvas, empty_rgba, show_grid)
from .headerproxy import get_proxy
from .player import PREVIEW_FLIP, SPOUT_INVERT, Player
from .spout_out import SpoutOutput

BG = (0.055, 0.055, 0.067)
CELL_BG = (0.02, 0.02, 0.027)
# Ceiling on preview repaints. Spout still receives every frame - this only caps
# how often the picture on screen is redrawn, which is the part that competes
# with the buttons for the GUI thread.
PREVIEW_INTERVAL = 1.0 / PREVIEW_FPS

# How often the render loop may run. A tick with nothing to do is a flag check,
# so this can be tight; it sets the worst-case delay between mpv producing a
# frame and Spout receiving it.
TICK_INTERVAL_MS = 4

SLOT_NAMES = ("A", "B")

GAP = 8          # logical px between the two preview cells
BORDER = 3       # inset of the picture inside its cell

IDLE_INTERVAL = IDLE_SENDER_MS / 1000.0

# The checkerboard behind a transparent empty slot. Dark and coarse on purpose
# (K): a bright, fine grid flashes on a dark stage and pulls the eye, and all
# it has to say is "nothing here". Squares in logical px.
GRID_SQUARE = 16
GRID_DARK = (0x1a, 0x1a, 0x1e)
GRID_LIGHT = (0x2c, 0x2c, 0x31)
# The grid texture grows in steps of this, so resizing the window does not
# rebuild it on every event.
GRID_STEP = 256


def empty_pixels(width: int, height: int) -> bytes:
    """RGBA8 bytes of an empty slot: transparent, or opaque black."""
    alpha = 255 if empty_rgba()[3] > 0 else 0
    return bytes((0, 0, 0, alpha)) * (max(0, width) * max(0, height))


def checker_pixels(width: int, height: int, square: int,
                   dark=GRID_DARK, light=GRID_LIGHT) -> bytes:
    """RGBA8 bytes of a checkerboard, first row starting with a dark square."""
    width, height, square = max(0, width), max(0, height), max(1, square)
    a = bytes(dark) + b"\xff"
    b = bytes(light) + b"\xff"
    repeats = width // (2 * square) + 1

    def row(first, second):
        return ((first * square + second * square) * repeats)[:width * 4]

    band = row(a, b) * square + row(b, a) * square
    return (band * (height // (2 * square) + 1))[:width * height * 4]


class Perf:
    """Wall-clock spent in each stage of the render loop.

    The loop has four parts and they fail in different ways, so a single fps
    number cannot tell you which one got slower: making the GL context current,
    mpv's own render into our FBO, the Spout copy, and the preview repaint.
    Timed separately, "the preview feels laggier" becomes one line naming the
    stage - which beats four plausible theories and a rebuild each.

    Off unless PERF_LOG_S is set, and the timing calls are skipped entirely
    when it is off rather than measured and discarded.

    **`paint` is not the whole cost of the preview.** It times paintGL, and
    QOpenGLWidget's buffer swap happens after paintGL returns, outside
    anything we can wrap. So a small `paint` is not evidence that repainting
    is cheap, and it is not a reason to remove the PREVIEW_FPS gate in tick().
    Said here because the number is exactly the kind that invites that
    conclusion, and the measurement cannot support it.
    """

    STAGES = ("gl", "render", "spout", "paint")

    def __init__(self, interval_s: float) -> None:
        self.on = interval_s > 0
        self.interval = max(0.0, float(interval_s))
        self._t0 = time.monotonic()
        self.reset()

    def reset(self) -> None:
        self.ticks = 0
        self.frames = 0
        self.repaints = 0
        # The longest wait between two calls of tick(), in seconds. Averages
        # hid the resize stutter: 0.9.9.18 showed 53-121 ticks per 5s while
        # resizing against 240 normally, but not how long a single gap got -
        # and one gap longer than a frame is exactly what Resolume shows.
        self.max_gap = 0.0
        self._last_call = None
        self.totals = dict.fromkeys(self.STAGES, 0.0)
        # Per slot, because "render is slow" and "slot B is slow" are
        # different faults with different fixes, and the two slots are
        # rendered in the same tick so the total hides which.
        self.slots = [0.0, 0.0]

    def tick_called(self, now: float) -> None:
        if self._last_call is not None:
            self.max_gap = max(self.max_gap, now - self._last_call)
        self._last_call = now

    def add(self, stage: str, seconds: float) -> None:
        self.totals[stage] += seconds

    def add_slot(self, index: int, seconds: float) -> None:
        while len(self.slots) <= index:
            self.slots.append(0.0)
        self.slots[index] += seconds
        self.totals["render"] += seconds

    def report(self, note: str = "") -> None:
        """Print and start again, if enough time has passed."""
        if not self.on:
            return
        now = time.monotonic()
        elapsed = now - self._t0
        if elapsed < self.interval:
            return
        self._t0 = now

        # Per FRAME, not per tick: a tick with nothing to do is a flag check,
        # and averaging those in makes every stage look fast while the app
        # stutters.
        per = max(1, self.frames)
        last_call = self._last_call
        stages = "  ".join(
            f"{name} {self.totals[name] / per * 1000:.2f}ms"
            for name in self.STAGES)
        share = sum(self.totals.values()) / elapsed * 100
        print(f"[chuutube] perf {elapsed:.1f}s: {self.ticks} ticks, "
              f"{self.frames} frames ({self.frames / elapsed:.1f}/s), "
              f"{self.repaints} repaints | {stages} | "
              f"max gap {self.max_gap * 1000:.0f}ms | "
              f"{share:.0f}% of the GUI thread{note}", file=sys.stderr)
        self.reset()
        # The gap runs across the report, not from it.
        self._last_call = last_call


class IdleFrame:
    """An empty texture, sent on a slot that has nothing loaded.

    A Spout sender does not exist until its first frame. That is why the names
    only turned up in Resolume after a video had played, and why they then
    stayed - releasing a sender is what removes it, and nothing does that until
    the app closes. Sending black from the moment the GL context exists puts
    both names in the source list at boot, which is when the show is patched.

    Quacks like Player.target on purpose: the same publish() path handles it,
    so there is one place that talks to Spout rather than two.
    """

    def __init__(self, width: int, height: int) -> None:
        self.w = max(2, int(width))
        self.h = max(2, int(height))
        self.tex = 0
        self.fbo = 0

    @property
    def valid(self) -> bool:
        return self.tex > 0

    def create(self) -> bool:
        if self.tex:
            return True
        try:
            self.tex = int(GL.glGenTextures(1))
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                               GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER,
                               GL.GL_LINEAR)
            # Zeroed explicitly. Passing None allocates without defining the
            # contents, and undefined on a GPU means "whatever that memory was
            # last used for" - which, published to Spout, is someone else's
            # frame buffer on a projector.
            # Transparent unless EMPTY_TRANSPARENT=0: the same colour a slot
            # clears to, so a slot looks the same before its first clip as
            # after Clear.
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, self.w, self.h, 0,
                            GL.GL_RGBA, GL.GL_UNSIGNED_BYTE,
                            empty_pixels(self.w, self.h))
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        except Exception as exc:
            print(f"[chuutube] no idle Spout frame ({exc}) - senders will "
                  "appear once a clip plays", file=sys.stderr)
            self.tex = 0
        return bool(self.tex)

    def destroy(self) -> None:
        if not self.tex:
            return
        try:
            GL.glDeleteTextures([self.tex])
        except Exception:
            pass
        self.tex = 0


class Checker:
    """The checkerboard texture, in an FBO so it can be blitted like a slot.

    One texture at least as big as the largest cell, grown in GRID_STEP steps.
    A blit cannot tile, and drawing the squares one by one costs a few hundred
    GL calls per cell per repaint.
    """

    def __init__(self) -> None:
        self.tex = 0
        self.fbo = 0
        self.w = 0
        self.h = 0
        self.square = 0
        self.failed = False

    def ensure(self, width: int, height: int, square: int) -> bool:
        if self.failed:
            return False
        if self.fbo and square == self.square \
                and width <= self.w and height <= self.h:
            return True
        grow = square == self.square
        w = _round_up(max(width, self.w if grow else 0), GRID_STEP)
        h = _round_up(max(height, self.h if grow else 0), GRID_STEP)
        self.destroy()
        try:
            self.tex = int(GL.glGenTextures(1))
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                               GL.GL_NEAREST)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER,
                               GL.GL_NEAREST)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0,
                            GL.GL_RGBA, GL.GL_UNSIGNED_BYTE,
                            checker_pixels(w, h, square))
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            self.fbo = int(GL.glGenFramebuffers(1))
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
            GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER,
                                      GL.GL_COLOR_ATTACHMENT0,
                                      GL.GL_TEXTURE_2D, self.tex, 0)
            status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
            if status != GL.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError(f"framebuffer status {status:#x}")
        except Exception as exc:
            print(f"[chuutube] no preview grid ({exc}) - empty slots show "
                  "dark instead", file=sys.stderr)
            self.destroy()
            self.failed = True
            return False
        self.w, self.h, self.square = w, h, square
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
                GL.glDeleteTextures([self.tex])
            except Exception:
                pass
            self.tex = 0
        self.w = self.h = 0


def _round_up(value: int, step: int) -> int:
    return max(step, -(-int(value) // step) * step)


class PreviewSurface(QOpenGLWidget):
    """Composites both players side by side and drives the render/send loop."""

    fpsChanged = Signal(float)
    glReady = Signal()

    def __init__(self, players: list[Player], spout: SpoutOutput, parent=None) -> None:
        super().__init__(parent)
        self.players = players
        self.spout = spout
        self._gl_ready = False
        self._shutting_down = False
        self._frames = 0
        self._fps_t0 = time.monotonic()
        self._last_paint = 0.0
        self._last_tick_call = 0.0
        self._dirty = False
        self._force_tick = True
        self._idle = IdleFrame(IDLE_SENDER_WIDTH, IDLE_SENDER_HEIGHT) \
            if IDLE_SENDERS else None
        self._last_idle = 0.0
        self._checker = Checker() if show_grid() else None
        self._perf = Perf(PERF_LOG_S)

        self.setMinimumHeight(220)
        # Clicking the preview parks keyboard focus somewhere harmless, which is
        # how "click elsewhere to deselect a button" is supposed to work.
        self.setFocusPolicy(Qt.ClickFocus)
        # We fill every pixel ourselves in paintGL. Telling Qt that stops it
        # painting a background underneath first - one less full-surface write
        # per repaint, and no pale flash in the newly exposed strip when the
        # window is dragged.
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)

        for p in players:
            p.frameReady.connect(self._on_frame_ready, Qt.QueuedConnection)

        # mpv's update callback fires far more often than there are frames -
        # it signals "there may be work", not "here is a picture". Ticking
        # directly from it meant hundreds of makeCurrent/send cycles a second on
        # the GUI thread, which is what made the whole interface treacly. The
        # callback now only sets a flag; this timer does the work, at most once
        # per interval, and only when there is something to do.
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_INTERVAL_MS)
        self._timer.timeout.connect(self.tick)

        self._fps_timer = QTimer(self)
        self._fps_timer.setInterval(1000)
        self._fps_timer.timeout.connect(self._emit_fps)

        # The repaint that the preview ceiling turned away, delivered once the
        # ceiling allows. Without it a frame drawn within 1/PREVIEW_FPS of the
        # previous paint was never shown unless another frame followed - fine
        # mid-clip, but Clear on the last playing slot produces exactly one
        # frame (the black) right after a video frame, and nothing after it.
        # K saw the old frame sit in the preview for seconds.
        self._late_paint = QTimer(self)
        self._late_paint.setSingleShot(True)
        self._late_paint.timeout.connect(self._paint_late)

        # Dragging the window puts Windows into its own modal move loop. Repaints
        # issued during it are handed to the compositor out of step with the move,
        # which is exactly what leaves a trail behind the window. So during a drag
        # we stop repainting the preview and let the last painted frame be moved
        # as-is; rendering and Spout output carry on untouched.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(max(1, DRAG_SETTLE_MS))
        self._settle.timeout.connect(self._end_interaction)
        self._interacting = False
        # True while Windows holds the window in its move loop (title bar
        # pressed, dragged or resized). Set by modalloop.ModalTicker.
        self._held = False
        self._held_frames = 0

    # -- GL lifecycle ---------------------------------------------------------

    def initializeGL(self) -> None:
        if self._gl_ready:
            return
        for p in self.players:
            p.attach_gl()
        if self._idle is not None:
            self._idle.create()
        self._gl_ready = True
        self._timer.start()
        self._fps_timer.start()
        self.glReady.emit()

    def shutdown(self) -> None:
        """Release GL + Spout resources in the right order, once."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._timer.stop()
        self._fps_timer.stop()
        if self._gl_ready:
            self.makeCurrent()
            for p in self.players:
                p.detach_gl()
            if self._idle is not None:
                self._idle.destroy()
            if self._checker is not None:
                self._checker.destroy()
            self.spout.shutdown()
            self.doneCurrent()
        else:
            self.spout.shutdown()
        for p in self.players:
            p.shutdown()
        get_proxy().shutdown()

    # -- render loop ----------------------------------------------------------

    @Slot()
    def _on_frame_ready(self) -> None:
        # Deliberately cheap: no GL, no Spout, no repaint. Just a note that the
        # next tick has something to look at.
        self._dirty = True

    @Slot()
    def tick(self) -> None:
        if not self._gl_ready or self._shutting_down:
            return
        now = time.monotonic()
        self._last_tick_call = now
        if self._perf.on:
            self._perf.tick_called(now)
        if self._interacting and DRAG_PAUSE_RENDER:
            return          # diagnostic: freezes Spout too, see config.py
        idle_due = self._idle_due(now)
        if not self._dirty and not self._force_tick and not idle_due:
            return
        self._dirty = False
        self._force_tick = False
        self._perf.ticks += 1

        clock = time.perf_counter if self._perf.on else None

        mark = clock() if clock else 0.0
        self.makeCurrent()
        if clock:
            self._perf.add("gl", clock() - mark)
            drew = []
            for index, player in enumerate(self.players):
                mark = clock()
                drew.append(player.render())
                self._perf.add_slot(index, clock() - mark)
        else:
            drew = [p.render() for p in self.players]
        targets, changed = self._publish_list(drew, idle_due)
        if any(drew):
            self._frames += 1
            self._perf.frames += 1
            if self._held:
                self._held_frames += 1
        # `any(drew)` as well as `any(changed)`: a slot that rendered but has
        # no valid target still goes through publish, which is where "is there
        # anything to send" has always been decided. Moving that decision up
        # here would be two places answering the same question.
        if any(drew) or any(changed):
            # Only publish what actually changed. A Spout receiver reads a
            # shared texture; re-sending identical pixels costs a GPU copy and
            # buys nothing.
            mark = clock() if clock else 0.0
            self.spout.publish(targets, SPOUT_INVERT, changed)
            if clock:
                self._perf.add("spout", clock() - mark)
        mark = clock() if clock else 0.0
        self.doneCurrent()
        if clock:
            self._perf.add("gl", clock() - mark)
        if idle_due:
            self._last_idle = now
        self._perf.report(self._perf_note())

        # Spout gets every frame; the on-screen preview does not need to. A
        # repaint costs a GUI-thread round trip, and at 60fps across two players
        # that is enough to make buttons feel unresponsive. The output is what
        # matters - the preview is a monitor.
        if any(drew) and not self._interacting:
            wait = PREVIEW_INTERVAL - (now - self._last_paint)
            if wait <= 0:
                self._paint_now(now)
            elif not self._late_paint.isActive():
                self._late_paint.start(max(1, int(wait * 1000 + 0.999)))

    def _paint_now(self, now: float) -> None:
        self._late_paint.stop()
        self._last_paint = now
        self._perf.repaints += 1
        if self._held:
            # update() only queues the paint, and Qt delivers it from a timer
            # that the move loop starves - which kept the preview frozen in
            # 0.9.9.32 even with the render timer running. repaint() paints
            # now, inside the WM_TIMER that called us.
            self.repaint()
        else:
            self.update()

    def set_held(self, held: bool) -> int:
        """Windows' move loop started (True) or ended (False).

        Returns the frames drawn during the hold, for the log line.
        """
        drawn = self._held_frames
        self._held = bool(held)
        self._held_frames = 0
        if not held:
            self._force_tick = True
            self.update()
        return drawn

    def held_tick(self) -> None:
        """One tick from the Windows timer while the window is held.

        Marks the loop dirty first. Normally mpv's frameReady signal does
        that, but it crosses threads as a queued Qt event, and queued events
        are exactly what the move loop does not deliver. 0.9.9.33's log
        showed the timer firing about 60 times a second while held and the
        picture still frozen: every tick found nothing marked and returned.
        Asking mpv directly costs one flag check per slot when there is no
        new frame.
        """
        self._dirty = True
        self.pump()

    def _paint_late(self) -> None:
        if not self._interacting and not self._shutting_down:
            self._paint_now(time.monotonic())

    def _idle_due(self, now: float) -> bool:
        """Is a slot waiting on its empty frame?

        Only asked of slots that have nothing to publish of their own. Once
        both have played something their own targets stay valid - a cleared
        slot renders black into its own FBO - so the idle beat stops entirely
        rather than waking the loop twice a second for the rest of the night.
        """
        if self._idle is None or not self._idle.valid:
            return False
        if (now - self._last_idle) < IDLE_INTERVAL:
            return False
        return any(p.target is None or not p.target.valid
                   for p in self.players)

    def _perf_note(self) -> str:
        """Per-slot size and render cost, since both are per slot per pixel."""
        per = max(1, self._perf.frames)
        parts = []
        for index, player in enumerate(self.players):
            target = player.target
            size = (f"{target.w}x{target.h}"
                    if target is not None and target.valid else "-")
            spent = self._perf.slots[index] if index < len(self._perf.slots) \
                else 0.0
            parts.append(f"{SLOT_NAMES[index]} {size} {spent / per * 1000:.1f}ms")
        return (f" | {', '.join(parts)}"
                f" | preview {self.width()}x{self.height()}")

    def _publish_list(self, drew, idle_due):
        """What to hand Spout this tick, and which entries actually moved.

        A slot with a real frame publishes that frame when it changed. A slot
        with nothing loaded publishes the empty idle texture, but only on the
        idle beat - otherwise an empty slot would re-copy black at the tick
        rate, which is 250 times a second for a picture that is not changing.
        """
        targets, changed = [], []
        for target, moved in zip((p.target for p in self.players), drew):
            if target is not None and target.valid:
                targets.append(target)
                changed.append(bool(moved))
            elif idle_due:
                targets.append(self._idle)
                changed.append(True)
            else:
                targets.append(None)
                changed.append(False)
        return targets, changed

    # -- geometry -------------------------------------------------------------

    def height_for_width(self, width: int | None = None,
                         aspect: float = PREVIEW_ASPECT) -> int:
        """Surface height that makes both cells exactly `aspect`, no bars.

        The two cells split the width, minus the gap between them, and each has
        a border drawn inside its own edges - so the picture area is narrower
        than half the surface by a fixed amount. Solving for the height here
        keeps the arithmetic in one place with the code that draws it.
        """
        width = self.width() if width is None else width
        cell_w = (max(1, int(width)) - GAP) // 2
        inner_w = max(1, cell_w - 2 * BORDER)
        return int(round(inner_w / max(0.01, aspect))) + 2 * BORDER

    def width_for_height(self, height: int,
                         aspect: float = PREVIEW_ASPECT) -> int:
        """The inverse: surface width whose cells are `aspect` at this height.

        Needed when the fitted window would be taller than the screen - then the
        height is the fixed quantity and the width has to give way.
        """
        inner_h = max(1, int(height) - 2 * BORDER)
        cell_w = inner_h * max(0.01, aspect) + 2 * BORDER
        return int(round(cell_w * 2 + GAP))

    # -- window interaction ---------------------------------------------------

    def pump(self) -> None:
        """Run a tick now if the timer is overdue.

        Called from the window's move and resize events. While Windows moves
        or resizes a window it runs its own message loop, and timer messages
        are the lowest priority there - delivered only when nothing else is
        queued, which during a drag is rarely. Measured (PERF_LOG_S, K's
        machine): 53-121 ticks per 5 s while resizing against ~240, with our
        own share of the GUI thread DOWN to 4-7%. The work was not slow; the
        timer was not firing. The resize events themselves do arrive, as
        often as the mouse moves, so they drive the loop when the timer will
        not. Still the GUI thread - if this is not enough, rendering moves to
        a thread of its own.
        """
        interval = (DRAG_TICK_MS if self._interacting else TICK_INTERVAL_MS) / 1000
        if time.monotonic() - self._last_tick_call >= interval:
            self.tick()

    def begin_interaction(self) -> None:
        """Called while the window is being moved or resized.

        Pausing only the preview repaint was not enough. The render loop itself
        wakes 250 times a second on the GUI thread, and every wakeup is one more
        thing Windows' modal move loop has to get past before it can move the
        window. So the loop slows down for the duration too - Spout keeps
        receiving frames, just at ~30fps instead of on-demand.
        """
        if DRAG_SETTLE_MS <= 0:
            return
        if not self._interacting:
            self._timer.setInterval(DRAG_TICK_MS)
        self._interacting = True
        self._settle.start()      # restarts on every move event

    def _end_interaction(self) -> None:
        self._interacting = False
        self._timer.setInterval(TICK_INTERVAL_MS)
        self._force_tick = True
        self.update()

    def _emit_fps(self) -> None:
        now = time.monotonic()
        elapsed = now - self._fps_t0
        if elapsed > 0:
            self.fpsChanged.emit(self._frames / elapsed)
        self._frames = 0
        self._fps_t0 = now

    # -- painting -------------------------------------------------------------

    def paintGL(self) -> None:
        if self._perf.on:
            mark = time.perf_counter()
            try:
                self._paint()
            finally:
                self._perf.add("paint", time.perf_counter() - mark)
            return
        self._paint()

    def _paint(self) -> None:
        default_fbo = self.defaultFramebufferObject()
        dpr = self.devicePixelRatioF()
        vw = max(1, int(round(self.width() * dpr)))
        vh = max(1, int(round(self.height() * dpr)))
        gap = max(1, int(round(GAP * dpr)))
        border = max(1, int(round(BORDER * dpr)))

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, default_fbo)
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glViewport(0, 0, vw, vh)
        GL.glClearColor(*BG, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)

        cell_w = (vw - gap) // 2
        for i, player in enumerate(self.players):
            cx = 0 if i == 0 else cell_w + gap
            cell = (cx, 0, cell_w, vh)
            # No accent border any more: it marked the slot the program sender
            # published, and that sender is gone. Which slot the keyboard
            # controls is shown on the panels underneath instead.
            self._fill(cell, CELL_BG)
            inner = (cell[0] + border, cell[1] + border,
                     max(1, cell[2] - 2 * border), max(1, cell[3] - 2 * border))

            target = player.target
            if self._checker is not None and _is_empty(player):
                self._draw_grid(inner, dpr, default_fbo)
                continue
            if not target.valid:
                continue
            dst = _fit(inner, target.w, target.h)
            if self._checker is not None and _leaves_room(dst, inner):
                # The clip is sent at its own shape, so whatever of the cell
                # it does not cover is not part of the output: show that as
                # the same grid as an empty slot, not as black bars that
                # Resolume never receives.
                self._draw_grid(inner, dpr, default_fbo)
            src = (0, 0, target.w, target.h)
            picture = (self._transparent_picture(player)
                       if self._checker is not None else None)
            if picture is not None:
                # Fixed canvas with SENDER_BACKGROUND=transparent: the bars
                # are see-through in Resolume, so the preview shows the grid
                # there and blits only the picture. A blit cannot blend, so
                # blitting the whole target would paint the bars black.
                self._draw_grid(inner, dpr, default_fbo)
                src = picture
                dst = _map_rect(picture, target.w, target.h, dst)
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, target.fbo)
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, default_fbo)
            x0, y0, x1, y1 = dst
            if PREVIEW_FLIP:
                y0, y1 = y1, y0
            GL.glBlitFramebuffer(*src, x0, y0, x1, y1,
                                 GL.GL_COLOR_BUFFER_BIT, GL.GL_LINEAR)

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, default_fbo)
        GL.glDisable(GL.GL_SCISSOR_TEST)

    @staticmethod
    def _transparent_picture(player):
        """The picture's rect in the target, when the bars around it are clear."""
        if SENDER_BACKGROUND != "transparent" or not canvas()[0]:
            return None
        rect = getattr(player, "picture_rect", None)
        return rect() if callable(rect) else None

    def _draw_grid(self, rect, dpr: float, default_fbo: int) -> None:
        """The checkerboard over the whole picture area of a cell."""
        x, y, w, h = (int(v) for v in rect)
        square = max(1, int(round(GRID_SQUARE * dpr)))
        if w <= 0 or h <= 0 or not self._checker.ensure(w, h, square):
            return
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._checker.fbo)
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, default_fbo)
        GL.glBlitFramebuffer(0, 0, w, h, x, y, x + w, y + h,
                             GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)

    def _fill(self, rect, colour) -> None:
        x, y, w, h = rect
        GL.glEnable(GL.GL_SCISSOR_TEST)
        GL.glScissor(int(x), int(y), max(0, int(w)), max(0, int(h)))
        GL.glClearColor(*colour, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glDisable(GL.GL_SCISSOR_TEST)


def _is_empty(player) -> bool:
    """Does this slot send the empty colour rather than a video frame?"""
    target = player.target
    return target is None or not target.valid or bool(getattr(player, "empty", False))


def _map_rect(rect, tw: int, th: int, dst):
    """rect, in a tw x th target, as it lands in dst (x0, y0, x1, y1)."""
    dx0, dy0, dx1, dy1 = dst
    kx = (dx1 - dx0) / tw
    ky = (dy1 - dy0) / th
    x0, y0, x1, y1 = rect
    return (dx0 + int(round(x0 * kx)), dy0 + int(round(y0 * ky)),
            dx0 + int(round(x1 * kx)), dy0 + int(round(y1 * ky)))


def _leaves_room(dst, cell) -> bool:
    """Does the fitted picture (x0, y0, x1, y1) leave part of cell uncovered?"""
    x0, y0, x1, y1 = dst
    # A pixel of rounding is not a bar worth a grid behind it.
    return (abs((x1 - x0) - int(cell[2])) > 1
            or abs((y1 - y0) - int(cell[3])) > 1)


def _fit(cell, vid_w: int, vid_h: int) -> tuple[int, int, int, int]:
    """Letterbox vid_w x vid_h inside cell; returns (x0, y0, x1, y1)."""
    cx, cy, cw, ch = cell
    if vid_w <= 0 or vid_h <= 0 or cw <= 0 or ch <= 0:
        return cx, cy, cx + max(0, cw), cy + max(0, ch)
    scale = min(cw / vid_w, ch / vid_h)
    w = max(1, int(round(vid_w * scale)))
    h = max(1, int(round(vid_h * scale)))
    x0 = cx + (cw - w) // 2
    y0 = cy + (ch - h) // 2
    return x0, y0, x0 + w, y0 + h
