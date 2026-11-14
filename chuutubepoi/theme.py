# SPDX-License-Identifier: GPL-3.0-or-later
"""Dark stylesheet, tuned for a dim booth rather than an office."""

ACCENT = "#00c78c"
ACCENT_DIM = "#0a7f5b"
# The URL row is colour-coded so the three buttons can be told apart without
# reading them - blue paste, green load, red clear. Only Paste is filled: it is
# the one pressed every time a clip changes, and three filled buttons side by
# side would be a traffic light rather than a hierarchy.
BLUE = "#4c9aff"
BLUE_DIM = "#1e4b8f"
RED = "#ff6b6b"
RED_DIM = "#7a2b2b"
BG = "#0e0e11"
PANEL = "#17171c"
PANEL_HI = "#1f1f26"
LINE = "#2b2b34"
TEXT = "#e6e6ea"
TEXT_DIM = "#8b8b98"
WARN = "#e0a458"

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-size: 13px;
}}
QFrame#panel {{
    background: {PANEL};
    border: 1px solid {LINE};
    border-radius: 6px;
}}
/* The slot the keyboard controls. Quieter than the old LIVE frame: it says
   where Space and the arrows go, nothing more. */
QFrame#panel[selected="true"] {{
    border: 1px solid {ACCENT_DIM};
}}
QLabel#slot {{
    font-weight: 700;
    font-size: 15px;
    color: {TEXT_DIM};
    padding: 0 4px;
}}
QLabel#slot[selected="true"] {{
    color: {ACCENT};
}}
QLabel#dim {{ color: {TEXT_DIM}; }}
QLabel#warn {{ color: {WARN}; }}
QLineEdit {{
    background: {BG};
    border: 1px solid {LINE};
    border-radius: 4px;
    padding: 5px 7px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus {{ border: 1px solid {ACCENT_DIM}; }}
QPushButton {{
    background: {PANEL_HI};
    border: 1px solid {LINE};
    border-radius: 4px;
    padding: 5px 11px;
    min-width: 26px;
}}
QPushButton:hover {{ background: #262630; }}
QPushButton:pressed {{ background: #101014; }}
QPushButton:checked {{
    background: {ACCENT_DIM};
    border-color: {ACCENT};
    color: #ffffff;
}}
/* Paste is the one button in the URL row that gets used every time a clip
   changes, so it is the one that is filled in. Load and Clear are outlined:
   coloured enough to find at a glance, quiet enough to stay auxiliary. */
/* Transport. Amber rather than the accent green, which marks the selected
   slot and the clip-is-ready state. */
QPushButton[role="transport"] {{
    color: {WARN};
    font-size: 15px;
}}
QPushButton[role="paste"] {{
    background: {BLUE_DIM};
    border-color: {BLUE};
    color: #ffffff;
    font-weight: 600;
    padding: 5px 15px;
    /* A hard minimum, like Load's. Without one the layout squeezed Paste
       and Clear below their text on a narrow window (tester, 0.9.9.25) while
       Load, the only button with a min-width, kept its size. */
    min-width: 44px;
}}
QPushButton[role="paste"]:hover {{ background: {BLUE}; color: #08131f; }}
QPushButton[role="paste"]:pressed {{ background: #143a70; color: #ffffff; }}

QPushButton[role="load"] {{
    border-color: {ACCENT_DIM};
    color: {ACCENT};
    padding: 5px 9px;
    /* Wide enough for "Loading 100%", so the URL field beside it does not
       shuffle on every percent while a load runs. */
    min-width: 88px;
}}
QPushButton[role="load"]:hover {{ background: {ACCENT_DIM}; color: #ffffff; }}
/* The clip behind this URL is already on disk, so Load will not have to wait.
   Filling the button it belongs to says that without adding another indicator. */
QPushButton[role="load"][ready="true"] {{
    background: {ACCENT_DIM};
    border-color: {ACCENT};
    color: #ffffff;
}}

QPushButton[role="clear"] {{
    border-color: {RED_DIM};
    color: {RED};
    padding: 5px 9px;
    min-width: 38px;
}}
QPushButton[role="clear"]:hover {{ background: {RED_DIM}; color: #ffffff; }}

/* A greyed-out control must read as unavailable, not as a colour choice - so
   this comes after the coloured rules and wins over all of them. */
QPushButton:disabled {{
    background: {PANEL_HI};
    border-color: {LINE};
    color: #55555f;
}}
/* Transport controls are pointer targets, not focus stops. Belt and braces
   alongside Qt.NoFocus: nothing here should ever look "armed" after a click. */
QPushButton:focus {{ outline: none; }}
QComboBox:focus {{ outline: none; }}
QComboBox {{
    background: {PANEL_HI};
    border: 1px solid {LINE};
    border-radius: 4px;
    padding: 4px 8px;
}}
QComboBox QAbstractItemView {{
    background: {PANEL_HI};
    border: 1px solid {LINE};
    selection-background-color: {ACCENT_DIM};
    outline: none;
}}
QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {LINE};
    border-radius: 3px;
    background: {BG};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QSlider::groove:horizontal {{
    height: 5px;
    background: {LINE};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 12px;
    margin: -5px 0;
    border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider:disabled::sub-page:horizontal {{ background: #33333c; }}
QToolTip {{
    background: {PANEL_HI};
    color: {TEXT};
    border: 1px solid {LINE};
    padding: 4px;
}}
"""
