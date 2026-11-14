# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal stand-ins for PySide6, OpenGL and mpv.

They exist so the logic in chuutubepoi can be exercised on a machine without a GPU,
a display, or Windows - which catches import errors, typos and wiring mistakes
long before anything touches real hardware. They are NOT a simulation of Qt.
"""

from __future__ import annotations

import sys
import types


class StubMeta(type):
    """Class-level attribute access (QSizePolicy.Expanding, Qt.Horizontal, ...)."""

    def __getattr__(cls, name):
        return _Flag(f"{cls.__name__}.{name}")


class Stub(metaclass=StubMeta):
    """Accepts any construction, any attribute, any call."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return _Anything()

    def __bool__(self):
        return True


class _Anything(Stub):
    """Callable, attributable, connectable - stands in for anything unmodelled."""

    def __call__(self, *args, **kwargs):
        return _Anything()


class _BoundSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot, *args, **kwargs):
        self._slots.append(slot)

    def disconnect(self, *args, **kwargs):
        self._slots.clear()

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)

    # Qt lets a signal be connected to another signal, which then re-emits.
    # Player does exactly that to hand a worker-thread signal to the GUI one,
    # so the stub has to be callable or the connection blows up on emit.
    def __call__(self, *args):
        self.emit(*args)


class Signal:
    def __init__(self, *types_):
        self._name = None

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        store = obj.__dict__.setdefault("_signals", {})
        return store.setdefault(self._name, _BoundSignal())


def Slot(*args, **kwargs):
    def deco(fn):
        return fn
    return deco


class QObject(Stub):
    def __init__(self, parent=None):
        self._parent = parent


class _Flag(str):
    """String-ish enum value that also supports Qt's bitwise flag syntax."""

    def __or__(self, other):
        return _Flag(f"{self}|{other}")

    __ror__ = __or__


class _QtNamespace:
    def __getattr__(self, name):
        return _Flag(f"Qt.{name}")


class QSettings(Stub):
    # Class-level, so a test can seed state the window will find when it builds
    # its own QSettings - which is the only way to exercise "restore what was
    # saved last time".
    seeded: dict = {}

    def __init__(self, *args, **kwargs):
        self._data = dict(QSettings.seeded)

    def value(self, key, default=None, type=None):
        return self._data.get(key, default)

    def setValue(self, key, value):
        self._data[key] = value

    def allKeys(self):
        return list(self._data)

    def sync(self):
        pass


class QElapsedTimer(Stub):
    def __init__(self):
        self._t = 0

    def start(self):
        self._t = 0

    def restart(self):
        self._t = 0

    def elapsed(self):
        return 10_000


class QTimer(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.timeout = _BoundSignal()
        # Set here: Stub answers any missing attribute with a truthy
        # placeholder, so an unset `running` would read as "active".
        self.running = False
        self.started_with = None

    def setInterval(self, ms):
        self.interval = ms

    def start(self, *a):
        self.running = True
        self.started_with = a

    def stop(self):
        self.running = False

    def isActive(self):
        return self.running

    @staticmethod
    def singleShot(_ms, fn):
        # No event loop here, so "next turn of the loop" is "now".
        fn()


class _FontMetrics:
    """Enough of QFontMetrics to exercise elision.

    Not a font: a fixed 7px per character. The question a test asks is "did the
    text get shortened to fit", and an unmodelled elidedText() answers
    _Anything, which is not a string and quietly becomes the label's text.
    """

    CHAR_PX = 7

    def elidedText(self, text, mode, width):
        limit = max(1, int(width) // self.CHAR_PX)
        return text if len(text) <= limit else text[:max(0, limit - 1)] + "\u2026"

    def horizontalAdvance(self, text):
        return len(text) * self.CHAR_PX


class _Widget(Stub, metaclass=StubMeta):
    """Widget base: real attributes win, everything else is a no-op."""

    def __init__(self, *args, **kwargs):
        self._props = {}
        # A list, not a flag: StubMeta answers every unknown attribute, so
        # hasattr() can never tell you whether something was actually called.
        self.resizes = []

    def setProperty(self, key, value):
        self._props[key] = value

    # Enabled/visible are modelled rather than swallowed by StubMeta: the app
    # greys the Load button while a fetch runs, greys PROGRAM when the program
    # sender is off, and hides the mute button when audio is not fetched. An
    # unmodelled isEnabled() answers _Anything, which is truthy, so every one of
    # those assertions would pass no matter what the code did.
    def setEnabled(self, value):
        self._enabled = bool(value)

    def isEnabled(self):
        return getattr(self, "_enabled", True)

    def setVisible(self, value):
        self._visible = bool(value)

    def isVisible(self):
        return getattr(self, "_visible", True)

    # Modelled because the tooltip is the only documentation several controls
    # have, and an unmodelled toolTip() answers _Anything - which is not a
    # string, so an assertion about its wording fails as an error rather than
    # telling you the wording is wrong.
    # Width matters here rather than being cosmetic: a QLabel's minimum width is
    # its full text, two panels sit side by side, and one clip with a very long
    # title would push the window wider than the screen. Modelled so a test can
    # say so.
    def setMinimumWidth(self, value):
        self._min_width = int(value)

    def minimumWidth(self):
        return getattr(self, "_min_width", 0)

    def fontMetrics(self):
        return _FontMetrics()

    def setToolTip(self, text):
        self._tooltip = text

    def toolTip(self):
        return getattr(self, "_tooltip", "")

    def setFocusPolicy(self, policy):
        self._focus_policy = policy

    def focusPolicy(self):
        return getattr(self, "_focus_policy", None)

    def property(self, key):
        return self._props.get(key)

    def window(self):
        return _current_window[0]

    def resize(self, w, h):
        self.resizes.append((w, h))

    def restoreGeometry(self, data):
        self.restored_geometry = data
        return bool(data)

    def saveGeometry(self):
        return b"geometry"

    # Qt's own event handlers, so an override can call super() the way it must.
    def moveEvent(self, event):
        pass

    def showEvent(self, event):
        pass

    def resizeEvent(self, event):
        pass

    def closeEvent(self, event):
        pass

    def width(self):
        return 1000

    def height(self):
        return 400

    def devicePixelRatioF(self):
        return 1.0

    def defaultFramebufferObject(self):
        return 0


_current_window = [Stub()]


class _MpvInstance:
    """Records what the app asks of mpv so tests can assert on it."""

    def __init__(self, **options):
        self.options = options
        self.props = {"pause": True, "mute": True, "time-pos": 0.0,
                      "duration": 0.0, "dwidth": 0, "dheight": 0}
        self.observers = {}
        self.loads = []
        self.seeks = []
        self.commands = []
        self.terminated = False

    def observe_property(self, name, handler):
        self.observers[name] = handler

    def __getitem__(self, key):
        return self.props.get(key)

    def __setitem__(self, key, value):
        self.props[key] = value

    def __getattr__(self, name):
        key = name.replace("_", "-")
        if key in self.__dict__.get("props", {}):
            return self.props[key]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in ("pause", "mute"):
            self.__dict__.setdefault("props", {})[name] = value
            return
        object.__setattr__(self, name, value)

    @property
    def pause(self):
        return self.props["pause"]

    @property
    def mute(self):
        return self.props["mute"]

    def loadfile(self, url, mode="replace", **options):
        self.loads.append((url, mode, options, self.props.get("start")))

    def seek(self, amount, reference="relative", precision="default"):
        self.seeks.append((amount, reference, precision))

    def command(self, *args):
        self.commands.append(args)

    def terminate(self):
        self.terminated = True

    def fire(self, prop, value):
        """Simulate an mpv property change notification."""
        self.props[prop] = value
        if prop in self.observers:
            self.observers[prop](prop, value)


def install() -> None:
    """Register the stub modules in sys.modules. Idempotent."""
    if "PySide6" in sys.modules:
        return

    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.QObject = QObject
    qtcore.Signal = Signal
    qtcore.Slot = Slot
    qtcore.Qt = _QtNamespace()
    qtcore.QTimer = QTimer
    qtcore.QSettings = QSettings
    qtcore.QElapsedTimer = QElapsedTimer
    qtcore.qVersion = lambda: "0.0-stub"

    qtgui = types.ModuleType("PySide6.QtGui")
    for name in ("QKeySequence", "QShortcut", "QSurfaceFormat", "QOpenGLContext",
                 "QOffscreenSurface", "QIcon", "QAction", "QPainter", "QColor"):
        setattr(qtgui, name, type(name, (Stub,), {}))

    class QDesktopServices(Stub):
        """Records what the app asked the system to open."""
        opened: list = []

        @staticmethod
        def openUrl(url):
            QDesktopServices.opened.append(url)
            return True
    qtgui.QDesktopServices = QDesktopServices

    class QUrl(Stub):
        def __init__(self, text=""):
            self.text = text

        @staticmethod
        def fromLocalFile(path):
            return QUrl("file://" + str(path))

        def toLocalFile(self):
            return self.text[len("file://"):]

        def __eq__(self, other):
            return isinstance(other, QUrl) and other.text == self.text

        def __hash__(self):
            return hash(self.text)
    qtcore.QUrl = QUrl

    qtwidgets = types.ModuleType("PySide6.QtWidgets")
    for name in ("QMainWindow", "QWidget", "QFrame", "QLabel", "QLineEdit",
                 "QPushButton", "QComboBox", "QCheckBox", "QVBoxLayout",
                 "QStyle", "QStyleOptionSlider",
                 "QHBoxLayout", "QMessageBox", "QSizePolicy"):
        setattr(qtwidgets, name, type(name, (_Widget,), {}))

    class _Clipboard:
        """The system clipboard, as far as the Paste button is concerned."""

        text_value = ""

        def text(self):
            return _Clipboard.text_value

        def setText(self, value):
            _Clipboard.text_value = value

    class QApplication(_Widget):
        # Modelled rather than left to StubMeta: an unmodelled clipboard()
        # answers a _Flag, and calling it raises - which would make every Paste
        # test pass through the error branch instead of the one being tested.
        clipboard_impl = _Clipboard()

        @staticmethod
        def clipboard():
            return QApplication.clipboard_impl

    qtwidgets.QApplication = QApplication
    qtwidgets._Clipboard = _Clipboard

    class QSlider(_Widget):
        def __init__(self, *a, **k):
            super().__init__()
            self._min, self._max, self._value = 0, 0, 0
            self.sliderPressed = _BoundSignal()
            self.sliderMoved = _BoundSignal()
            self.sliderReleased = _BoundSignal()
            # Real attribute: Stub.__getattr__ answers _Anything for anything
            # unset, so a getattr() default would never be reached.
            self.presses_forwarded = 0

        def setRange(self, lo, hi):
            self._min, self._max = lo, hi

        def setValue(self, v):
            self._value = v

        # SeekSlider moves the handle with this before handing the press to
        # QSlider, so the stub has to model it or dragging cannot be tested.
        def setSliderPosition(self, v):
            self._value = v

        def sliderPosition(self):
            return self._value

        # SeekSlider.mousePressEvent must forward the press to QSlider - that
        # one missing call is what killed dragging. Modelled so the override can
        # call super() the way it must, and so its absence would be a test error
        # rather than something StubMeta quietly swallows.
        def mousePressEvent(self, event):
            self.presses_forwarded += 1

        def value(self):
            return self._value

        def minimum(self):
            return self._min

        def maximum(self):
            return self._max

    qtwidgets.QSlider = QSlider

    class QComboBox(_Widget):
        def __init__(self, *a, **k):
            super().__init__()
            self._items = []
            self._index = 0
            self.currentIndexChanged = _BoundSignal()

        def addItem(self, label, data=None):
            self._items.append((label, data))

        def setCurrentIndex(self, i):
            self._index = i
            self.currentIndexChanged.emit(i)

        def currentIndex(self):
            return self._index

        def itemData(self, i):
            return self._items[i][1]

        def currentData(self):
            return self._items[self._index][1] if self._items else None

        def findData(self, data):
            for i, (_, d) in enumerate(self._items):
                if d == data:
                    return i
            return -1

    qtwidgets.QComboBox = QComboBox

    class QLineEdit(_Widget):
        def __init__(self, *a, **k):
            super().__init__()
            self._text = ""
            self._focused = False
            self.selected = False
            self.returnPressed = _BoundSignal()

        def setText(self, t):
            self._text = t

        def text(self):
            return self._text

        def clear(self):
            self._text = ""

        # Enough of the focus/selection model to exercise UrlEdit for real.
        def hasFocus(self):
            return self._focused

        def setFocus(self, *a):
            if not self._focused:
                self._focused = True
                self.focusInEvent(None)

        def focusInEvent(self, event):
            self._focused = True

        def mousePressEvent(self, event):
            self._focused = True

        def selectAll(self):
            self.selected = True

        def deselect(self):
            self.selected = False

    qtwidgets.QLineEdit = QLineEdit

    class QPushButton(_Widget):
        def __init__(self, *a, **k):
            super().__init__()
            self._checked = False
            self._text = a[0] if a else ""
            self.clicked = _BoundSignal()

        def setCheckable(self, v):
            pass

        def setChecked(self, v):
            self._checked = v

        def isChecked(self):
            return self._checked

        def setText(self, t):
            self._text = t

        def text(self):
            return self._text

    qtwidgets.QPushButton = QPushButton

    class QCheckBox(_Widget):
        def __init__(self, *a, **k):
            super().__init__()
            self._checked = False
            self.toggled = _BoundSignal()

        def setChecked(self, v):
            self._checked = v
            self.toggled.emit(v)

        def isChecked(self):
            return self._checked

    qtwidgets.QCheckBox = QCheckBox

    class QLabel(_Widget):
        def __init__(self, text="", *a, **k):
            super().__init__()
            self._text = text

        def setText(self, t):
            self._text = t

        def text(self):
            return self._text

    qtwidgets.QLabel = QLabel

    qtglw = types.ModuleType("PySide6.QtOpenGLWidgets")
    qtglw.QOpenGLWidget = type("QOpenGLWidget", (_Widget,), {})

    pyside = types.ModuleType("PySide6")
    pyside.__version__ = "0.0-stub"
    pyside.QtCore = qtcore
    pyside.QtGui = qtgui
    pyside.QtWidgets = qtwidgets
    pyside.QtOpenGLWidgets = qtglw

    gl = types.ModuleType("OpenGL.GL")

    class _GL:
        def __getattr__(self, name):
            if name.isupper() or name.startswith("GL_"):
                return 0x1000
            return lambda *a, **k: 1

    _glinst = _GL()
    for name in dir(_glinst):
        pass
    gl.__getattr__ = _glinst.__getattr__
    opengl = types.ModuleType("OpenGL")
    opengl.__version__ = "0.0-stub"
    opengl.GL = gl

    mpv_mod = types.ModuleType("mpv")
    mpv_mod.MPV = _MpvInstance
    mpv_mod.MpvRenderContext = type("MpvRenderContext", (Stub,), {})
    mpv_mod.MpvGlGetProcAddressFn = lambda fn: fn

    sys.modules.update({
        "PySide6": pyside,
        "PySide6.QtCore": qtcore,
        "PySide6.QtGui": qtgui,
        "PySide6.QtWidgets": qtwidgets,
        "PySide6.QtOpenGLWidgets": qtglw,
        "OpenGL": opengl,
        "OpenGL.GL": gl,
        "mpv": mpv_mod,
    })
