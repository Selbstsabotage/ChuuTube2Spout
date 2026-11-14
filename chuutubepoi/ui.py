# SPDX-License-Identifier: GPL-3.0-or-later
"""Main window: two player panels under a shared preview surface."""

from __future__ import annotations

import re
import sys

from PySide6.QtCore import (QElapsedTimer, QSettings, Qt, QTimer, QUrl, Signal,
                            Slot)
from PySide6.QtGui import (QColor, QDesktopServices, QKeySequence, QPainter,
                           QShortcut)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSizePolicy, QSlider, QStyle,
    QStyleOptionSlider, QVBoxLayout, QWidget,
)

from . import APP_NAME, LEGACY_APP_NAMES, __version__
from . import cookies as cookie_store
from .player import Player

from .potprovider import get_provider
from pathlib import Path

from .config import (CACHE_DIR, CACHE_MAX_AGE_H, CACHE_MAX_GB,
                     CLEAR_CACHE_ON_EXIT, FETCH_AUDIO,
                     PREFETCH_DEBOUNCE_MS, SENDER_A, SENDER_B, SENDER_FIT,
                     WINDOW_FILL_SCREEN, WINDOW_SCALE, WINDOW_WIDTH,
                     canvas, output_description)
from .modalloop import ModalTicker
from .quality import DEFAULT_HEIGHT, PRESETS
from .spout_out import create_output
from .surface import PreviewSurface
from .theme import ACCENT_DIM, LINE, STYLESHEET

# Transport glyphs, each followed by U+FE0E (VARIATION SELECTOR-15, "render
# this as text, not as an emoji"). Without it Windows picks Segoe UI Emoji for
# these codepoints and draws its own colour - which is what "the pause button
# is blue and I cannot unhighlight it" actually was. A colour font also ignores
# a stylesheet `color`, so the selector has to come first for the amber below
# to apply at all.
PLAY_GLYPH = "\u25b6\ufe0e"
PAUSE_GLYPH = "\u23f8\ufe0e"
START_GLYPH = "\u23ee\ufe0e"     # back to the start

SLOTS = ("A", "B")
SCRUB_THROTTLE_MS = 180
# mpv reports time-pos on every frame. Redrawing a slider and a label that often,
# for two players, starves the event loop - which is what makes the clock appear
# to skip. Ten updates a second is smooth to the eye and nearly free.
TIME_UPDATE_MS = 100

# Wide enough that each preview cell is about 640px across, so two 16:9 clips
# sit side by side at a useful size. The height is worked out from this at
# startup rather than guessed - see MainWindow.fit_preview.
DEFAULT_WIDTH = 1320
URL_MIN_WIDTH = 110
DEFAULT_HEIGHT_GUESS = 760


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def settings_path() -> Path:
    """The settings.txt in use, or where it would be.

    Named in full wherever a message tells someone to edit it (K, 1.1): in
    the Mac app it is hidden in ~/Library, and "settings.txt" alone sent the
    tester looking next to the app.
    """
    from .bootstrap import project_root
    from .config import SETTINGS_FILE
    return Path(SETTINGS_FILE) if SETTINGS_FILE else project_root() / "settings.txt"


def open_folder(folder: Path) -> bool:
    """Show a folder in the Finder or the Explorer."""
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))))


SETTINGS_BUTTON = "Open settings folder"
PRIVACY_BUTTON = "Open Full Disk Access"
# System Settings, at Privacy & Security > Full Disk Access.
FULL_DISK_ACCESS_URL = ("x-apple.systempreferences:"
                        "com.apple.preference.security?Privacy_AllFiles")


def mac_app_path() -> Path | None:
    """ChuuTubePoi.app, when running as the Mac app: what to choose after +."""
    from .bootstrap import is_mac_app
    if not is_mac_app():
        return None
    return Path(sys.executable).resolve().parents[2]


def add_settings_button(box) -> None:
    """A button on a message box that shows the folder holding settings.txt.

    The box closes when it is pressed, as with any of its buttons.
    """
    button = box.addButton(SETTINGS_BUTTON, QMessageBox.ActionRole)
    folder = settings_path().parent
    button.clicked.connect(lambda: open_folder(folder))


_DOMAIN = re.compile(r"^(?:[\w-]+\.)+[a-z]{2,}(?:[/:?#]|$)", re.I)


def worth_fetching(text: str) -> bool:
    """Is this worth fetching ahead, before anyone presses Load?

    A link (anything with ://, or a bare domain), or a file that is there.
    Not a stray key: "1", "2" and "f" typed over a link each started a
    yt-dlp run (tester, 1.1). Load itself still tries anything.
    """
    text = (text or "").strip()
    if not text:
        return False
    if "://" in text or _DOMAIN.match(text):
        return True
    try:
        return Path(text).expanduser().exists()
    except (OSError, ValueError):
        return False


def keyboard_help(platform: str | None = None) -> str:
    """The shortcuts, as the keyboard in front of the user labels them.

    K, 1.1: on a Mac the help said Ctrl+1 where Qt wants ⌘1, and Home,
    which a Mac keyboard does not have.
    """
    mac = (platform or sys.platform) == "darwin"
    jump = "⌘1 / ⌘2" if mac else "Ctrl+1 / Ctrl+2"
    start = "⌘← (or fn+←)" if mac else "Home"
    return (
        "The keys act on the highlighted slot. Pick it with 1 / 2, or by "
        "using anything in a slot.<br>"
        "1 / 2 - select slot A / B<br>"
        "Space - play / pause<br>"
        "M - audio on / off (one slot at a time)<br>"
        "← / → - seek 5s &nbsp;&nbsp; Shift+←/→ - seek 60s<br>"
        f"{start} - back to the start<br>"
        f"{jump} - jump to a URL field<br>"
        "Esc - leave a URL field, so the keys above work again, and undo "
        "what was typed there (Enter loads and leaves)<br>"
        "F - resize the window so both previews are exactly 16:9<br>")


def log_line() -> str:
    """Where this run is being logged, for the help box. Empty if it is not.

    In the help box because that is where someone looks when something has
    gone wrong and they want to tell you about it - which is the whole reason
    the log exists.
    """
    from .logfile import path as log_path
    current = log_path()
    if current is None:
        return ""
    text = ("<b>Log</b><br>This session is being written to "
            f"<code>{current}</code>.<br>Attach it to a bug report.<br><br>")
    # Both systems: where settings.txt is, with a button to open its folder.
    text += (f"<b>Settings</b><br><code>{settings_path()}</code><br>"
             f"{SETTINGS_BUTTON} shows it.<br><br>")
    return text


def restyle(widget: QWidget) -> None:
    """Re-evaluate a stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def no_focus(*widgets: QWidget) -> None:
    """Stop a control from keeping keyboard focus after it is clicked.

    Clicking a button used to leave it outlined and highlighted for the rest of
    the session, because nothing else in the window wanted focus back. Mid-set
    that reads as "this is armed", and worse, Space then went to the button
    instead of the play/pause shortcut. Transport controls are pointer targets;
    only the URL fields need focus, so only they keep it.
    """
    for widget in widgets:
        widget.setFocusPolicy(Qt.NoFocus)


class ElidedLabel(QLabel):
    """A label that gives way instead of pushing the window wider.

    A QLabel with no word wrap reports its full text as BOTH its preferred and
    its minimum width, and a layout's minimum becomes the window's minimum -
    which Qt enforces by growing the window. So one long message anywhere in
    the interface could make the window wider than the monitor. On a tester's
    laptop a Spout failure (whose text carries the whole exception) did exactly
    that, and the window spilled onto the second screen.

    Eliding the text alone does not fix it, and can make it worse: elide to the
    current width, and the label's preferred width becomes that width, so the
    layout grows a little, so the next elide fits more text, and the window
    ratchets open until the whole message fits. The fix has to be in the size
    hints, which is why this is a widget and not a helper function.

    `floor` is the width below which it will not shrink - enough to show that
    something is there and to be worth hovering over. The full text is always
    on the tooltip.
    """

    def __init__(self, text: str = "", parent=None, floor: int = 70,
                 cap: int = 420) -> None:
        super().__init__(parent)
        self._full = ""
        self._floor = max(8, int(floor))
        self._cap = max(self._floor, int(cap))
        self.setTextFormat(Qt.PlainText)
        # An explicit minimum, not just a minimumSizeHint: Qt lets the hint be
        # overridden by an explicit minimum and not the other way round, and
        # this is the number that has to hold when a layout is deciding how
        # small the window may be.
        self.setMinimumWidth(self._floor)
        self.setText(text)

    # -- text ---------------------------------------------------------------

    def setText(self, text) -> None:                      # noqa: N802 (Qt)
        text = "" if text is None else str(text)
        self._full = text
        self.setToolTip(text)
        self._re_elide()

    def full_text(self) -> str:
        return self._full

    def _re_elide(self) -> None:
        try:
            width = max(self._floor, int(self.width()) - 2)
            shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight,
                                                  width)
        except Exception:
            shown = self._full
        QLabel.setText(self, shown)

    # -- sizing -------------------------------------------------------------

    def minimumSizeHint(self):                            # noqa: N802 (Qt)
        hint = super().minimumSizeHint()
        hint.setWidth(self._floor)
        return hint

    def sizeHint(self):                                   # noqa: N802 (Qt)
        hint = super().sizeHint()
        # Capped, or a long message still asks for more room than the screen
        # has and every other widget in the row is squeezed to nothing.
        try:
            hint.setWidth(max(self._floor, min(self._cap, int(hint.width()))))
        except Exception:
            pass
        return hint

    def resizeEvent(self, event) -> None:                 # noqa: N802 (Qt)
        super().resizeEvent(event)
        self._re_elide()


class SeekSlider(QSlider):
    """QSlider that jumps to where you click, and then lets you drag from there.

    The first version handled the click itself and returned without calling
    super(). That is what made clicking work and dragging not: QSlider only
    enters its drag state from its own mousePressEvent, so sliderPressed never
    fired, the handle was never grabbed, and sliderMoved and sliderReleased
    never arrived. Clicking looked fine because the custom path did the whole
    job; dragging had nothing behind it at all.

    So: move the handle under the cursor, then hand the press to QSlider. The
    press now lands on the handle, Qt starts a normal drag, and both gestures
    work through the machinery Qt already has.
    """

    clickedAt = Signal(int)

    def _value_at(self, x: float) -> int:
        """Map a click x to a value the way the style lays the groove out.

        Not x/width: the handle has width, so the usable groove is shorter than
        the widget and offset by half a handle at each end. Getting this wrong
        puts the handle slightly off the cursor, and Qt then decides the press
        missed it and page-steps instead of dragging - which is the same bug
        again, just intermittent and only near the ends.
        """
        try:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            style = self.style()
            groove = style.subControlRect(QStyle.CC_Slider, opt,
                                          QStyle.SC_SliderGroove, self)
            handle = style.subControlRect(QStyle.CC_Slider, opt,
                                          QStyle.SC_SliderHandle, self)
            span = max(1, groove.width() - handle.width())
            offset = int(x) - groove.x() - handle.width() // 2
            return QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), offset, span)
        except Exception:
            # No real style to ask (or a stub in the tests): the plain
            # proportion is close enough to place the handle.
            frac = float(x) / max(1, self.width())
            value = self.minimum() + round(frac * (self.maximum() - self.minimum()))
            return max(self.minimum(), min(self.maximum(), int(value)))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.maximum() > self.minimum():
            value = self._value_at(event.position().x())
            # setSliderPosition, not setValue: it is the handle we need under
            # the cursor before Qt does its hit test.
            self.setSliderPosition(value)
            self.clickedAt.emit(value)
        # Always, even after handling it: this call is what starts the drag.
        super().mousePressEvent(event)


STRIP_HEIGHT = 2


# set_strip() value for "fetching, no percentage yet": a segment sweeps along
# the strip instead of filling it.
STRIP_BUSY = -1.0
BUSY_SEGMENT = 0.25         # of the track's width
BUSY_STEP_MS = 30
BUSY_SWEEP_MS = 1200        # one pass, left to right


def strip_rects(width: int, height: int, fraction: float):
    """(track, filled) as (x, y, w, h) along the bottom inside the border."""
    fraction = min(1.0, max(0.0, fraction))
    x, w = 2, max(0, width - 4)
    y = max(0, height - STRIP_HEIGHT - 2)
    return (x, y, w, STRIP_HEIGHT), (x, y, int(w * fraction), STRIP_HEIGHT)


def busy_rect(width: int, height: int, phase: float):
    """The sweeping segment at `phase` (0-1), clipped to the track.

    It enters from the left edge and leaves off the right, so the motion
    reads as travelling rather than bouncing.
    """
    (x, y, w, h), _ = strip_rects(width, height, 0.0)
    seg = max(1, int(w * BUSY_SEGMENT))
    start = x - seg + int((w + seg) * (phase % 1.0))
    left, right = max(x, start), min(x + w, start + seg)
    return (left, y, max(0, right - left), h)


class UrlEdit(QLineEdit):
    """A URL field that selects everything the moment you click into it.

    Swapping a clip means replacing the whole URL, never editing part of one -
    so the useful thing to do on a click is select all of it, ready to be typed
    or pasted over. Qt's default puts a caret where you clicked, which costs a
    double-click or a Ctrl+A first, every time.

    The selection is applied after the click has been handled: doing it during
    the press just gets undone when Qt sets the caret.
    """

    # Clicking or tabbing into the field picks its slot for the keyboard.
    focused = Signal()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 0.0-1.0 while the clip in the box downloads in the BACKGROUND,
        # STRIP_BUSY before the first percentage, None otherwise. A strip
        # along the bottom edge rather than anything on the Load button: the
        # button is the action, and "Load 42%" read as if it were already
        # loading. A foreground load shows on the button instead.
        self._strip: float | None = None
        # The few seconds of page, token and format lookup before yt-dlp
        # prints a percentage used to show nothing, so Paste got pressed again
        # and again. A sweeping segment fills that gap from the first moment.
        self._phase = 0.0
        self._animating = False
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(BUSY_STEP_MS)
        self._busy_timer.timeout.connect(self._advance)

    def set_strip(self, fraction: float | None) -> None:
        if fraction == self._strip:
            return
        self._strip = fraction
        busy = fraction is not None and fraction < 0
        if busy and not self._animating:
            self._phase = 0.0
            self._busy_timer.start()
        elif not busy and self._animating:
            self._busy_timer.stop()
        self._animating = busy
        self.update()

    def _advance(self) -> None:
        self._phase = (self._phase + BUSY_STEP_MS / BUSY_SWEEP_MS) % 1.0
        self.update()

    def paintEvent(self, event) -> None:                  # noqa: N802 (Qt)
        super().paintEvent(event)
        if self._strip is None:
            return
        width, height = self.width(), self.height()
        track, filled = strip_rects(width, height, max(0.0, self._strip))
        if self._animating:
            filled = busy_rect(width, height, self._phase)
        painter = QPainter(self)
        painter.fillRect(*track, QColor(LINE))
        if filled[2] > 0:
            painter.fillRect(*filled, QColor(ACCENT_DIM))
        painter.end()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        # What Esc goes back to. Arriving selects everything, so the next key
        # replaces the link: 1, 2 or F pressed for the shortcuts did (tester,
        # 1.1, the log shows "1", "2" and "f" fetched as links).
        self._entry_text = self.text()
        self.focused.emit()
        if self.text():
            QTimer.singleShot(0, self.selectAll)

    def mousePressEvent(self, event) -> None:
        already_focused = self.hasFocus()
        super().mousePressEvent(event)
        # Clicking a field that already has focus is a deliberate act - putting
        # the caret somewhere - so only re-select when arriving from elsewhere.
        if not already_focused and self.text():
            QTimer.singleShot(0, self.selectAll)

    def keyPressEvent(self, event) -> None:
        # Esc leaves the field. While it has the keyboard, 1, 2, Space and M
        # are typed into it, so there was no way back to the shortcuts but
        # the mouse (K, 1.1). What was typed since arriving is undone.
        if event.key() == Qt.Key_Escape:
            entry = getattr(self, "_entry_text", None)
            if entry is not None and self.text() != entry:
                self.setText(entry)
            self.leave()
            event.accept()
            return
        super().keyPressEvent(event)

    def leave(self) -> None:
        """Give the keyboard back to the window's shortcuts."""
        self.deselect()
        self.clearFocus()


class PlayerPanel(QFrame):
    """Controls for one player."""

    # "Make me the slot the keyboard controls" - from any control in the panel.
    selectRequested = Signal(int)
    audioToggled = Signal(int)

    def __init__(self, index: int, player: Player, parent=None) -> None:
        super().__init__(parent)
        self.index = index
        self.player = player
        self.audio_enabled = False
        self._scrubbing = False
        self._loading = False
        self._selected = False
        # What the background fetch for the URL in the box is doing - "",
        # "preparing" or "ready" - and how far it has got; and how far a load
        # started with the Load button has got. Drawn by _render_fetch.
        self._prefetch_kind = ""
        self._prefetch_pct: int | None = None
        self._load_pct: int | None = None
        self._duration = 0.0
        self._scrub_clock = QElapsedTimer()
        self._scrub_clock.start()
        self._time_clock = QElapsedTimer()
        self._time_clock.start()

        self.setObjectName("panel")
        self.setProperty("selected", False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(7)

        # -- row 1: slot letter, url, load
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.slot_label = QLabel(SLOTS[index])
        self.slot_label.setObjectName("slot")
        self.slot_label.setProperty("selected", False)
        self.url_edit = UrlEdit()
        self.url_edit.focused.connect(self._select)
        self.url_edit.setPlaceholderText("Paste a YouTube URL (or any mpv-playable URL / file path)")
        # Enough to show the start of a link. Explicit for the same reason as
        # the button minimums in theme.py: a hint alone gets squeezed.
        self.url_edit.setMinimumWidth(URL_MIN_WIDTH)
        self.url_edit.returnPressed.connect(self.load)
        # Enter has done its job: the shortcuts work again straight away.
        self.url_edit.returnPressed.connect(self.url_edit.leave)
        self.url_edit.textEdited.connect(self._on_url_edited)

        # Paste is what actually gets pressed when a clip changes, so it is the
        # prominent one; Load and Clear are quieter beside it.
        self.paste_btn = QPushButton("Paste")
        self.paste_btn.setProperty("role", "paste")
        self.paste_btn.setToolTip(
            "Replace this URL with the clipboard, and start fetching it")
        self.paste_btn.clicked.connect(self.paste)
        self.load_btn = QPushButton("Load")
        self.load_btn.setProperty("role", "load")
        self.load_btn.setToolTip("Play the URL in the box")
        self.load_btn.clicked.connect(self.load)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setProperty("role", "clear")
        self.clear_btn.setToolTip("Empty the box and stop this slot")
        self.clear_btn.clicked.connect(self.clear)
        no_focus(self.paste_btn, self.load_btn, self.clear_btn)
        row1.addWidget(self.slot_label)
        row1.addWidget(self.url_edit, 1)
        row1.addWidget(self.paste_btn)
        row1.addWidget(self.load_btn)
        row1.addWidget(self.clear_btn)
        outer.addLayout(row1)

        # A typed or edited URL is only worth fetching once it stops changing;
        # the Paste button does not wait, because it knows the URL is complete.
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(PREFETCH_DEBOUNCE_MS)
        self._prefetch_timer.timeout.connect(self._prefetch_now)

        # -- row 2: seekbar
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.slider = SeekSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setEnabled(False)
        self.slider.sliderPressed.connect(self._on_scrub_start)
        self.slider.sliderMoved.connect(self._on_scrub_move)
        self.slider.sliderReleased.connect(self._on_scrub_end)
        self.slider.clickedAt.connect(self._on_slider_clicked)
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("dim")
        self.time_label.setMinimumWidth(92)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row2.addWidget(self.slider, 1)
        row2.addWidget(self.time_label)
        outer.addLayout(row2)

        # -- row 3: transport
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        # U+FE0E is the variation selector for TEXT presentation. Without it
        # Windows hands these codepoints to Segoe UI Emoji, which draws its own
        # colours - the button looks permanently highlighted in blue and no
        # stylesheet `color` can touch it, because a colour font ignores it.
        self.start_btn = QPushButton(START_GLYPH)
        self.start_btn.setProperty("role", "transport")
        self.start_btn.setToolTip("Back to the start (Home)")
        self.start_btn.clicked.connect(self.jump_to_start)
        self.play_btn = QPushButton(PLAY_GLYPH)
        self.play_btn.setProperty("role", "transport")
        self.play_btn.setToolTip("Play / pause")
        self.play_btn.clicked.connect(self.toggle_pause)
        self.mute_btn = QPushButton("\U0001f507")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Audio on / off - one slot at a time; turning "
                                 "this on turns the other slot's off")
        self.mute_btn.clicked.connect(self.toggle_audio)
        # With FETCH_AUDIO off there is no audio track to unmute, so the button
        # would be a control that does nothing.
        self.mute_btn.setVisible(FETCH_AUDIO)
        self.quality_box = QComboBox()
        for label, height in PRESETS:
            self.quality_box.addItem(label, height)
        self.quality_box.setCurrentIndex(
            max(0, [h for _, h in PRESETS].index(DEFAULT_HEIGHT)))
        self.quality_box.setToolTip(
            "Target quality. If the video has nothing at or below this, "
            "the next rung up is used.")
        self.quality_box.currentIndexChanged.connect(self._on_quality_changed)
        # ElidedLabel rather than QLabel: titles arrive in here and some of
        # them are a paragraph long, and two panels sit side by side.
        self.status_label = ElidedLabel("idle", floor=60)
        self.status_label.setObjectName("dim")
        self.status_label.setSizePolicy(QSizePolicy.Expanding,
                                        QSizePolicy.Preferred)
        no_focus(self.start_btn, self.play_btn, self.mute_btn, self.quality_box)
        row3.addWidget(self.start_btn)
        row3.addWidget(self.play_btn)
        row3.addWidget(self.mute_btn)
        row3.addWidget(self.quality_box)
        row3.addWidget(self.status_label, 1)
        outer.addLayout(row3)

        player.timeChanged.connect(self._on_time, Qt.QueuedConnection)
        player.durationChanged.connect(self._on_duration, Qt.QueuedConnection)
        player.pauseChanged.connect(self._on_pause, Qt.QueuedConnection)
        player.statusChanged.connect(self._on_status, Qt.QueuedConnection)
        player.loadingChanged.connect(self._on_loading, Qt.QueuedConnection)
        player.prefetchChanged.connect(self._set_prefetch_state,
                                       Qt.QueuedConnection)
        player.prefetchProgress.connect(self._on_prefetch_progress,
                                        Qt.QueuedConnection)
        player.loadProgress.connect(self._on_load_progress, Qt.QueuedConnection)

    # -- actions --------------------------------------------------------------

    def load(self) -> None:
        self._select()
        url = self.url_edit.text().strip()
        if not url or self._loading:
            # A 1080p fetch takes seconds, during which the button looked idle.
            # Pressing it again started a second download of the same clip -
            # the first one's result was then discarded as stale, so the only
            # visible effect was that everything took twice as long.
            return
        self._duration = 0.0
        # Set immediately, not on the queued loadingChanged that follows: two
        # clicks can land in the same event-loop turn, and the signal would not
        # have been delivered yet for the second one to see.
        self._set_loading(True)
        self.player.load(url)

    def paste(self) -> None:
        """Replace the box with the clipboard and start fetching it.

        Overwrite, not insert: swapping a clip means replacing the whole URL,
        which is the same reasoning that makes UrlEdit select-all on focus.
        """
        self._select()
        text = ""
        try:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                text = (clipboard.text() or "").strip()
        except Exception as exc:            # no clipboard on this platform
            self.status_label.setText(f"could not read the clipboard: {exc}")
            return
        if not text:
            self.status_label.setText("clipboard is empty")
            return
        self.url_edit.setText(text)
        self._prefetch_timer.stop()
        self._prefetch_now()

    def _on_url_edited(self, _text: str) -> None:
        # Restarts the countdown on every keystroke, so a URL being typed is
        # fetched once, when it settles - not once per character.
        self._set_prefetch_state("")
        self._prefetch_timer.start()

    def _prefetch_now(self) -> None:
        url = self.url_edit.text().strip()
        if worth_fetching(url):
            self.player.prefetch(url)

    @Slot(str)
    def _set_prefetch_state(self, state: str) -> None:
        """Show what the background fetch for the URL in the box is doing.

        Background and foreground look different on purpose. Caching ahead
        (nobody pressed Load) is a thin strip along the bottom of the URL
        field, filling as it downloads; once it is on disk the Load button
        fills, saying Load will be instant. Pressing Load is the foreground:
        the button itself says "Loading 42%". The status line stays with the
        clip that is playing - a clip caching behind a live one would
        otherwise either hide or overwrite it.
        """
        self._prefetch_kind = state
        self._prefetch_pct = None
        self.load_btn.setProperty("ready", state == "ready")
        restyle(self.load_btn)
        self.load_btn.setToolTip({
            "preparing": "Fetching this clip now, so Load will not have to wait",
            "ready": "Already downloaded - Load starts it immediately",
        }.get(state, "Play the URL in the box"))
        self._render_fetch()
        # The status line belongs to whatever is playing. Only borrow it when
        # the slot has nothing to say for itself.
        if self.player.stream is None and not self._loading:
            self.status_label.setText(
                {"preparing": "fetching ahead...", "ready": "ready"}.get(state, "idle"))

    @Slot(int)
    def _on_prefetch_progress(self, percent: int) -> None:
        # Stored whatever the state: _render_fetch only shows it while
        # "preparing", and the next state change resets it, so a percentage
        # that arrives late cannot surface on a finished or replaced fetch.
        self._prefetch_pct = percent
        self._render_fetch()

    @Slot(int)
    def _on_load_progress(self, percent: int) -> None:
        # Only shown while loading, and reset when a load starts or ends.
        self._load_pct = percent
        self._render_fetch()

    def _render_fetch(self) -> None:
        if self._loading:
            # Foreground. A Load that joined a background fetch is foreground
            # too - the strip hands over to the button.
            self.load_btn.setText("Loading" if self._load_pct is None
                                  else f"Loading {self._load_pct}%")
            self.url_edit.set_strip(None)
            return
        self.load_btn.setText("Load")
        if self._prefetch_kind == "preparing":
            self.url_edit.set_strip(STRIP_BUSY if self._prefetch_pct is None
                                    else self._prefetch_pct / 100.0)
        else:
            self.url_edit.set_strip(None)

    def clear(self) -> None:
        self._prefetch_timer.stop()
        self._set_prefetch_state("")
        self.url_edit.clear()
        self.player.stop()
        self._duration = 0.0
        self.slider.setRange(0, 0)
        self.slider.setEnabled(False)
        self.time_label.setText("0:00 / 0:00")

    def toggle_pause(self) -> None:
        self._select()
        self.player.toggle_pause()

    def jump_to_start(self) -> None:
        self._select()
        # Exact, not keyframe: "the start" means the first frame, and the
        # nearest keyframe to 0 can be a second or more in on some encodes.
        self.player.seek(0, exact=True)

    def toggle_audio(self) -> None:
        self._select()
        self.set_audio(not self.audio_enabled)
        self.audioToggled.emit(self.index)

    def set_audio(self, enabled: bool) -> None:
        """Set the speaker without announcing it - for the window to switch
        the other slot off when this one comes on."""
        self.audio_enabled = enabled
        self.mute_btn.setChecked(enabled)
        self.mute_btn.setText("\U0001f50a" if enabled else "\U0001f507")

    # -- selection: which slot the keyboard controls --------------------------

    def _select(self) -> None:
        self.selectRequested.emit(self.index)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.setProperty("selected", selected)
        self.slot_label.setProperty("selected", selected)
        restyle(self)
        restyle(self.slot_label)

    def mousePressEvent(self, event) -> None:            # noqa: N802 (Qt)
        # A click on the panel's own background, between the controls.
        self._select()
        super().mousePressEvent(event)

    # -- scrubbing ------------------------------------------------------------

    def _on_scrub_start(self) -> None:
        self._select()
        self._scrubbing = True

    def _on_scrub_move(self, value: int) -> None:
        self._update_time_label(value / 1000.0)
        # Rough live scrub, rate-limited so a network stream isn't hammered.
        if self._scrub_clock.elapsed() > SCRUB_THROTTLE_MS:
            self._scrub_clock.restart()
            self.player.seek(value / 1000.0, exact=False)

    def _on_scrub_end(self) -> None:
        self._scrubbing = False
        self.player.seek(self.slider.value() / 1000.0, exact=True)

    def _on_slider_clicked(self, value: int) -> None:
        self.player.seek(value / 1000.0, exact=True)

    # -- player signals -------------------------------------------------------

    @Slot(float)
    def _on_time(self, value: float) -> None:
        if self._scrubbing:
            return
        if self._time_clock.elapsed() < TIME_UPDATE_MS:
            return
        self._time_clock.restart()
        self.slider.setValue(int(value * 1000))
        self._update_time_label(value)

    def _set_loading(self, loading: bool) -> None:
        self._loading = loading
        self._load_pct = None
        self.load_btn.setEnabled(not loading)
        self._render_fetch()

    @Slot(bool)
    def _sync_play_button(self) -> None:
        """Make the glyph agree with mpv without waiting for a change.

        observe_property fires on a CHANGE. A clip that loads unpaused never
        changes `pause`, so no event arrived and the button kept the play arrow
        it was built with - reading as "paused" over a playing clip. The first
        click then looked like it did nothing, because it set pause=True while
        the glyph already showed play.
        """
        self._on_pause(self.player.paused)

    def _on_loading(self, loading: bool) -> None:
        self._set_loading(loading)
        if not loading:
            self._sync_play_button()

    @Slot(float)
    def _on_duration(self, value: float) -> None:
        value = float(value or 0.0)
        if value <= 0 and self._duration > 0:
            # A transient zero from mpv, not a live stream. Disabling the bar
            # here is what stranded it mid-clip; a real change of clip resets
            # _duration in load() and clear().
            return
        self._duration = value
        seekable = self._duration > 0
        self.slider.setEnabled(seekable)
        self.slider.setRange(0, int(self._duration * 1000) if seekable else 0)
        self._update_time_label(self.slider.value() / 1000.0)

    @Slot(bool)
    def _on_pause(self, paused: bool) -> None:
        self.play_btn.setText(PLAY_GLYPH if paused else PAUSE_GLYPH)

    @Slot(str)
    def _on_status(self, text: str) -> None:
        """Show the status. The label does the eliding and the tooltip."""
        self.status_label.setText(text)

    def _update_time_label(self, current: float) -> None:
        if self._duration > 0:
            self.time_label.setText(f"{fmt_time(current)} / {fmt_time(self._duration)}")
        else:
            self.time_label.setText(f"{fmt_time(current)} / live")

    def _on_quality_changed(self, index: int) -> None:
        self.player.set_quality(self.quality_box.itemData(index))


class MainWindow(QMainWindow):
    # The start-up cookie check's answer, from its thread to this one.
    cookieVerdict = Signal(bool, str)
    # A Syphon sender left the Metal path (macOS): sender name, reason.
    syphonFallback = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.cookieVerdict.connect(self._show_cookie_verdict)
        self._cookie_box = None
        # Queued: the sender reports from inside a frame, with the GL context
        # current. The box opens after the frame, not in the middle of it.
        self.syphonFallback.connect(self._show_syphon_fallback,
                                    Qt.QueuedConnection)
        self._fallback_box = None
        self.setWindowTitle(APP_NAME)
        self.setStyleSheet(STYLESHEET)
        self.settings = QSettings(APP_NAME, APP_NAME)

        # Started before anything can be loaded: without it YouTube serves an
        # opening chunk and refuses the rest.
        get_provider().start()
        self.players = [Player(name) for name in SLOTS]
        # Spout on Windows, Syphon on macOS (since 1.1). Same interface either way.
        self.spout = create_output()
        if hasattr(self.spout, "set_fallback_handler"):
            self.spout.set_fallback_handler(self.syphonFallback.emit)
        self.surface = PreviewSurface(self.players, self.spout)
        # Keeps the render loop - and so Spout - running while the title bar
        # is held. See modalloop.py.
        self._modal = ModalTicker(self.surface.held_tick, self.surface.set_held)
        # Which slot Space, M, the arrows and Home act on. Picked with 1 / 2 or
        # by using any control in a slot; shown as a highlight, sent nowhere.
        self.selected_index = 0

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(9)

        self.surface.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.surface, 1)

        panels_row = QHBoxLayout()
        panels_row.setSpacing(8)
        self.panels = []
        for i, player in enumerate(self.players):
            panel = PlayerPanel(i, player)
            panel.selectRequested.connect(self.select_slot)
            panel.audioToggled.connect(self._on_audio_toggled)
            panels_row.addWidget(panel, 1)
            self.panels.append(panel)
        root.addLayout(panels_row)

        root.addWidget(self._build_status_bar())
        self.setCentralWidget(central)

        self.surface.fpsChanged.connect(self._on_fps)
        self._install_shortcuts()
        restored = self._restore_settings()
        self.select_slot(self.selected_index)
        self.apply_audio()
        self._fitted = False
        if not restored:
            # Only a starting point. The height that makes both cells 16:9
            # depends on font metrics we cannot know until the widgets are laid
            # out, so showEvent measures it and corrects.
            self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT_GUESS)

    def showEvent(self, event) -> None:
        """Fit the previews once, after the first layout has settled.

        This has to happen here rather than in __init__: before the window is
        shown the surface has no real size to measure, so the arithmetic would
        be done against a placeholder. It runs even when a saved geometry was
        restored - your width and screen position are kept, but a height left
        over from an older version (or from dragging the window about) is not a
        reason to start the session letterboxed.
        """
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            QTimer.singleShot(0, self._first_fit)
            self._start_cookie_check()

    def _start_cookie_check(self) -> None:
        """Once per start: is the browser set for cookies signed in?

        In a thread: reading a cookie store takes a moment, and on a Mac a
        Chrome store asks the keychain first. The window and the render loop
        must not wait for either.
        """
        from .config import COOKIE_WARNING, COOKIES_FROM_BROWSER
        if not COOKIE_WARNING:
            return
        import threading

        def run():
            ok, problem = cookie_store.check(COOKIES_FROM_BROWSER)
            self.cookieVerdict.emit(ok, problem)
        threading.Thread(target=run, name="cookie-check", daemon=True).start()

    @Slot(bool, str)
    def _show_cookie_verdict(self, ok: bool, problem: str) -> None:
        """Log the answer; if it is bad, say it once, without blocking."""
        if ok:
            print("[chuutube] cookies: signed in to YouTube", file=sys.stderr)
            return
        print(f"[chuutube] cookies: {problem}", file=sys.stderr)
        # Not exec(): a modal loop here would hold up nothing visible, but
        # show() keeps the start exactly as it was, with a note on top.
        from .cookies import MAC_PRIVACY_START
        privacy = problem.startswith(MAC_PRIVACY_START)
        text = problem
        if privacy and mac_app_path() is not None:
            # The file dialog behind + needs to be pointed at the app.
            text += f"\n\nThe app is here:\n{mac_app_path()}"
        text += ("\n\nTo stop this message: COOKIE_WARNING=0 in settings.txt."
                 f"\n\nsettings.txt is here:\n{settings_path()}")
        box = QMessageBox(QMessageBox.Warning, f"{APP_NAME} - YouTube login",
                          text, QMessageBox.Ok, self)
        if privacy:
            button = box.addButton(PRIVACY_BUTTON, QMessageBox.ActionRole)
            button.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(FULL_DISK_ACCESS_URL)))
        add_settings_button(box)
        box.setModal(False)
        box.show()
        self._cookie_box = box

    @Slot(str, str)
    def _show_syphon_fallback(self, name: str, reason: str) -> None:
        """Say once per start that transparency is lost; do not block.

        Both senders fall back for the same cause, so the second report
        would only repeat the first.
        """
        if self._fallback_box is not None:
            return
        box = QMessageBox(
            QMessageBox.Warning, f"{APP_NAME} - Syphon",
            "Transparency will not work in this session.\n\n"
            "The Metal output could not start, so the app sends through "
            "Syphon's OpenGL output instead. There, the bars of a 4:3 clip "
            "arrive black instead of transparent.\n\n"
            f"Reason ({name}): {reason}\n\n"
            "Please send the log file from the logs folder to the developer.",
            QMessageBox.Ok, self)
        box.setModal(False)
        box.show()
        self._fallback_box = box

    def _first_fit(self) -> None:
        """The startup fit, which is also where WINDOW_SCALE is applied."""
        available = self._available_size()
        floor = self._layout_floor()
        too_small = floor is not None and (self.width() < floor[0]
                                           or self.height() < floor[1])
        width = None
        if WINDOW_WIDTH:
            # A fixed width is the most specific instruction there is, so it
            # wins over WINDOW_SCALE and over a size saved last session.
            width = WINDOW_WIDTH
        elif WINDOW_FILL_SCREEN:
            # Cannot overshoot, and does not need changing on another monitor.
            width = available[0] if available else None
        elif WINDOW_SCALE != 1.0:
            # An explicit scale is an instruction, so it wins over a width left
            # over from last session. At 1 (the default) nothing is overridden
            # and a size you left the window at comes back untouched.
            width = int(round(DEFAULT_WIDTH * WINDOW_SCALE))
        elif too_small:
            # A saved size the controls do not fit into. The tester's window
            # came back at 689 px (0.9.9.25 log) - most likely the size he
            # left it at while testing resize - and restoring it squashed the
            # buttons. Start at the default instead;
            # a saved size the controls do fit into is still kept.
            width = DEFAULT_WIDTH
        if width is not None and floor is not None:
            # Below the floor the minimum would only grow the window again
            # after the fit, and leave the previews off 16:9.
            width = max(width, floor[0])
        self.fit_preview(width)
        # After the fit, not before: applying a minimum resizes a window that
        # is below it, and fit_preview measures the chrome from the window
        # and the preview as they are - a half-applied resize skews that.
        if floor is not None:
            self.setMinimumSize(*floor)
        self._clamp_to_screen()
        self._report_size(available, floor)

    def _layout_floor(self) -> tuple[int, int] | None:
        """The smallest window the controls fit into, from the layout.

        Qt does not reliably hold a top-level window to this by itself: the
        tester's restored 689 px window was narrower than its own panels need,
        and the box layouts squeezed every widget without a hard minimum.
        Setting it explicitly makes it a real minimum, for the restore and for
        dragging the window smaller.
        """
        try:
            hint = self.minimumSizeHint()
            width, height = int(hint.width()), int(hint.height())
        except (TypeError, ValueError, AttributeError):
            return None           # a stub, or a Qt build that disagrees
        if width <= 0 or height <= 0:
            return None
        return width, height

    def fit_preview(self, target_width: int | None = None) -> None:
        """Resize the window so both preview cells are exactly 16:9.

        Normally only the height moves - the width is whatever the window
        already is, which is what makes this useful after dragging the window
        wider (the F shortcut). `target_width` overrides that.
        """
        if self.surface.width() <= 1:
            return
        # Everything that is not preview: margins either side, and the two
        # control panels plus the status bar below.
        chrome_w = self.width() - self.surface.width()
        chrome_h = self.height() - self.surface.height()

        width = self.width() if target_width is None else int(target_width)
        available = self._available_size()
        if available:
            width = min(width, available[0])
        height = chrome_h + self.surface.height_for_width(
            max(1, width - chrome_w))

        if available and height > available[1]:
            # Too tall for the screen - so the height is now the fixed quantity
            # and the width has to give way, or we would reintroduce the black
            # bars this whole thing exists to remove.
            height = available[1]
            width = chrome_w + self.surface.width_for_height(
                max(1, height - chrome_h))

        if (width, height) != (self.width(), self.height()):
            self.resize(max(1, width), max(1, height))

    def _clamp_to_screen(self) -> None:
        """Last resort: keep the whole window on one screen.

        fit_preview already refuses to ask for more than the screen allows, but
        asking is not the only way a window grows - Qt enforces the layout's
        minimum size regardless of what anyone resized to, so a single widget
        demanding more width than the monitor has drags the window out past the
        edge and onto whatever is next to it. The labels are elided now so that
        should not happen again; this is here because the failure mode is a
        window a tester cannot reach the controls of, and the check costs
        nothing.

        Reports rather than fixing silently: a window that quietly shrinks is
        the next confusing bug report.
        """
        try:
            self._do_clamp()
        except Exception as exc:
            # Qt builds disagree about what geometry calls return, and a
            # cosmetic safety net is not worth failing to open over.
            print(f"[chuutube] could not check the window against the screen "
                  f"({exc})", file=sys.stderr)

    def _do_clamp(self) -> None:
        screen = None
        for candidate in self._screens():
            screen = candidate
            break
        if screen is None:
            return
        area = screen.availableGeometry()

        frame = self.frameGeometry()
        width = min(frame.width(), area.width())
        height = min(frame.height(), area.height())
        if (width, height) != (frame.width(), frame.height()):
            overflow = self.minimumSizeHint()
            print(f"[chuutube] window clamped to the screen: "
                  f"{frame.width()}x{frame.height()} -> {width}x{height} "
                  f"(layout wants at least {overflow.width()}x"
                  f"{overflow.height()})", file=sys.stderr)
            # The frame includes the title bar and borders, which resize() does
            # not set - take the difference off or the clamp is a few pixels
            # short every time.
            chrome_w = frame.width() - self.width()
            chrome_h = frame.height() - self.height()
            self.resize(max(1, width - chrome_w), max(1, height - chrome_h))
            frame = self.frameGeometry()

        x = min(max(frame.x(), area.x()), area.x() + area.width() - frame.width())
        y = min(max(frame.y(), area.y()), area.y() + area.height() - frame.height())
        if (x, y) != (frame.x(), frame.y()):
            self.move(x, y)

    def _screens(self):
        """Screens worth asking about our size, best guess first."""
        candidates = []
        try:
            candidates.append(self.screen())
        except Exception:
            pass
        try:
            from PySide6.QtGui import QGuiApplication
            candidates.append(QGuiApplication.primaryScreen())
        except Exception:
            pass
        return [s for s in candidates if s is not None]

    def _available_size(self) -> tuple[int, int] | None:
        """Usable screen area in logical pixels, or None if we cannot find out.

        Asks the window's own screen first and falls back to the primary one.
        The fallback matters: at the moment this runs the window may not have
        been placed on a screen yet, and a missing answer here is not harmless -
        it is the difference between "opens at twice the size" and "opens wider
        than the monitor".
        """
        for screen in self._screens():
            try:
                geometry = screen.availableGeometry()
                width, height = int(geometry.width()), int(geometry.height())
            except (TypeError, ValueError, AttributeError):
                continue          # a stub, or a Qt build that disagrees
            if width > 0 and height > 0:
                return width, height
        return None

    def _report_size(self, available, floor=None) -> None:
        """Say what was decided, so a surprising window is one line to explain."""
        where = f"{available[0]}x{available[1]} usable" if available else \
                "screen size unknown - COULD NOT CLAMP"
        if WINDOW_WIDTH:
            asked = f"width {WINDOW_WIDTH}"
        else:
            asked = "scale " + ("max" if WINDOW_FILL_SCREEN
                                else f"{WINDOW_SCALE:g}")
        least = f", minimum {floor[0]}x{floor[1]}" if floor else ""
        print(f"[chuutube] window: {asked}, "
              f"opened {self.width()}x{self.height()}{least} (screen: {where})",
              file=sys.stderr)

    # -- bottom bar -----------------------------------------------------------

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(12)

        self.senders_label = ElidedLabel(floor=90, cap=520)
        self.senders_label.setObjectName("dim")

        self.fps_label = QLabel("0.0 fps")
        self.fps_label.setObjectName("dim")

        self.help_btn = QPushButton("?")
        self.help_btn.setToolTip("Keyboard shortcuts")
        self.help_btn.clicked.connect(self._show_help)

        # The cache holds a plain, timestamped record of what was played, so
        # whether it survives closing the app is a choice worth making in
        # sight of the user rather than in a config file they never open.
        self.clear_cache_box = QCheckBox("Clear cache on exit")
        self.clear_cache_box.setToolTip(
            "Off: clips are kept, so a set prepared earlier still plays "
            f"without re-downloading.\nThey are removed after "
            f"{CACHE_MAX_AGE_H:g}h anyway, and once the folder passes "
            f"{CACHE_MAX_GB:g} GB.\n"
            "On: the folder is emptied when the app closes, leaving no record "
            "of what was played.")
        self.clear_cache_box.toggled.connect(self._on_clear_cache_toggled)

        no_focus(self.clear_cache_box, self.help_btn)
        row.addWidget(self.clear_cache_box)
        row.addWidget(self.senders_label, 1)
        # Both of these carry an exception message, so both are elided. This
        # row is where the window-widening bug lived: a Spout failure prints
        # the whole load error, and a plain QLabel makes that the window's
        # minimum width.
        if not self.spout.available:
            warn = ElidedLabel(self.spout.unavailable_reason, floor=90)
            warn.setObjectName("warn")
            row.addWidget(warn)
        if not get_provider().active:
            pot = ElidedLabel("no PO token provider - YouTube downloads "
                              "will stall", floor=90)
            pot.setObjectName("warn")
            pot.setToolTip(get_provider().status)
            row.addWidget(pot)
        row.addWidget(self.fps_label)
        row.addWidget(self.help_btn)
        self._update_senders_label()
        return bar

    def _update_senders_label(self) -> None:
        """Names, and the size they publish at.

        The size belongs here because it is a property of the senders rather
        than of any clip - it is what Resolume will show whatever you load, and
        the only place in the window that says so.
        """
        text = f"{self.spout.kind} out:  " + "   ".join(self.spout.names())
        text += f"   @ {output_description()}"
        width, height = canvas()
        if width and height:
            if SENDER_FIT != "fit":
                text += f" ({SENDER_FIT})"
        self.senders_label.setText(text)

    def _on_clear_cache_toggled(self, enabled: bool) -> None:
        # Remembered per machine. The settings.txt value is only the starting
        # position for someone who has never touched the checkbox.
        self.settings.setValue("clear_cache_on_exit", bool(enabled))

    @Slot(float)
    def _on_fps(self, fps: float) -> None:
        self.fps_label.setText(f"{fps:0.1f} fps")

    # -- selection and audio --------------------------------------------------

    @Slot(int)
    def select_slot(self, index: int) -> None:
        self.selected_index = index
        for i, panel in enumerate(self.panels):
            panel.set_selected(i == index)

    @Slot(int)
    def _on_audio_toggled(self, index: int) -> None:
        """One slot audible at a time: turning one on turns the other off."""
        if self.panels[index].audio_enabled:
            for i, panel in enumerate(self.panels):
                if i != index and panel.audio_enabled:
                    panel.set_audio(False)
        self.apply_audio()

    def apply_audio(self) -> None:
        for player, panel in zip(self.players, self.panels):
            player.muted = not panel.audio_enabled

    # -- shortcuts ------------------------------------------------------------

    def _install_shortcuts(self) -> None:
        def add(seq, fn):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(fn)

        add("Space", lambda: self.players[self.selected_index].toggle_pause())
        add("1", lambda: self.select_slot(0))
        add("2", lambda: self.select_slot(1))
        add("M", lambda: self.panels[self.selected_index].toggle_audio())
        add("Left", lambda: self.players[self.selected_index].seek_relative(-5))
        add("Right", lambda: self.players[self.selected_index].seek_relative(5))
        add("Shift+Left", lambda: self.players[self.selected_index].seek_relative(-60))
        add("Shift+Right", lambda: self.players[self.selected_index].seek_relative(60))
        add("Home", lambda: self.panels[self.selected_index].jump_to_start())
        if sys.platform == "darwin":
            # Mac keyboards have no Home key (fn+← sends it). Qt reads Ctrl
            # as ⌘ on a Mac, so this is ⌘←, the Mac's own "to the start".
            add("Ctrl+Left", lambda: self.panels[self.selected_index].jump_to_start())
        # Qt reads Ctrl as ⌘ on a Mac: this is ⌘1 / ⌘2 there.
        add("Ctrl+1", lambda: self.panels[0].url_edit.setFocus())
        add("Ctrl+2", lambda: self.panels[1].url_edit.setFocus())
        add("F", self.fit_preview)

    def _show_help(self) -> None:
        box = QMessageBox(
            QMessageBox.Information, APP_NAME,
            "<b>Keyboard</b><br>" + keyboard_help() + "<br>"
            f"<b>{self.spout.kind}</b><br>"
            f"'{SENDER_A}' and '{SENDER_B}' are always live: switch and "
            "crossfade between them inside Resolume.<br><br>"
            + log_line() +
            "Both slots keep decoding at all times, so switching is instant - "
            "that is what the second slot is for."
            f"<br><br><b>{APP_NAME} {__version__}</b> - free software under the "
            "GNU GPL v3 or later, with ABSOLUTELY NO WARRANTY. "
            "See the LICENSE file.",
            QMessageBox.Ok, self)
        add_settings_button(box)
        box.exec()

    # -- settings -------------------------------------------------------------

    def _restore_settings(self) -> bool:
        """Reload saved state. Returns whether a window geometry was restored.

        The caller needs to know: it used to resize to a fixed default straight
        afterwards, which quietly threw away the size you had left the window at.
        """
        self._migrate_settings()
        geo = self.settings.value("geometry")
        restored = False
        if geo is not None:
            try:
                restored = bool(self.restoreGeometry(geo))
            except Exception:
                restored = False
        for i, panel in enumerate(self.panels):
            url = self.settings.value(f"slot{i}/url", "", type=str)
            panel.url_edit.setText(url)
            height = self.settings.value(f"slot{i}/quality", DEFAULT_HEIGHT)
            height = None if height in (None, "", "None") else int(height)
            idx = panel.quality_box.findData(height)
            if idx >= 0:
                panel.quality_box.setCurrentIndex(idx)
            panel.player.set_quality(height)
        self.clear_cache_box.setChecked(self.settings.value(
            "clear_cache_on_exit", CLEAR_CACHE_ON_EXIT, type=bool))
        try:
            self.selected_index = min(1, max(0, int(self.settings.value("selected", 0))))
        except (TypeError, ValueError):
            self.selected_index = 0
        return restored

    def _migrate_settings(self) -> None:
        """Carry the window state over from the app's earlier name, once.

        QSettings keys are namespaced by application name, so a rename would
        otherwise silently reset the window size and both URL fields. The
        newest earlier name that left anything wins.
        """
        if self.settings.value("geometry") is not None:
            return
        for name in LEGACY_APP_NAMES:
            try:
                legacy = QSettings(name, name)
                keys = list(legacy.allKeys())
            except Exception:
                continue
            if not keys:
                continue
            for key in keys:
                self.settings.setValue(key, legacy.value(key))
            try:
                self.settings.sync()
            except Exception:
                pass
            return

    def _save_settings(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        for i, panel in enumerate(self.panels):
            self.settings.setValue(f"slot{i}/url", panel.url_edit.text())
            data = panel.quality_box.currentData()
            self.settings.setValue(f"slot{i}/quality", "None" if data is None else data)
        self.settings.setValue("selected", self.selected_index)

    # -- window interaction ---------------------------------------------------

    def nativeEvent(self, eventType, message):            # noqa: N802 (Qt)
        # Every Windows message to this window passes through here, so it is
        # one attribute lookup and out for all but three of them.
        modal = getattr(self, "_modal", None)
        if modal is not None and modal.handle(eventType, message):
            return True, 0
        return super().nativeEvent(eventType, message)

    def moveEvent(self, event) -> None:
        # Fires repeatedly while the window is dragged. begin_interaction does
        # nothing unless DRAG_SETTLE_MS is set; pump() keeps Spout fed while
        # Windows' move loop starves the render timer (see pump).
        super().moveEvent(event)
        self.surface.begin_interaction()
        self.surface.pump()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.surface.begin_interaction()
        self.surface.pump()

    def closeEvent(self, event) -> None:
        self._save_settings()
        self.surface.shutdown()
        get_provider().stop()
        # The exported cookie jar is a live login. It exists for the length of
        # a session and no longer.
        cookie_store.cleanup()
        # After surface.shutdown(), which terminates mpv - otherwise every file
        # is still open and nothing can be deleted.
        if self.clear_cache_box.isChecked():
            from .downloader import clear as clear_cache
            clear_cache(Path(CACHE_DIR))
        event.accept()
