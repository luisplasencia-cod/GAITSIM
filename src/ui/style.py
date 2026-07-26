"""
style.py

Centralized visual constants for touch-friendly UI design, sized for
the project's 10.1" touchscreen (1280x800 effective resolution).

Change values here to adjust sizing across the entire application
without touching individual screens.

Light/dark theming (2026-07-26+, Luis's explicit request for a live
toggle — see src/ui/theme_manager.py and the new button in
main_window.py's nav bar): every SIZE-only constant below (button
dimensions, font sizes, spacing) stays a plain module-level constant,
unchanged — those never differ between themes. Every constant that
bakes in an actual COLOR is now a zero-argument FUNCTION instead (same
UPPER_SNAKE_CASE name, so it reads like a constant at the call site,
just with `()` added) — it reads the CURRENTLY ACTIVE palette
(_PALETTES[_active_theme]) fresh on every call, instead of a value
frozen at import time. This is what makes a live theme switch possible
at all: `from module import COLOR_X` binds a snapshot in Python, so a
constant computed once at import time can never change later no matter
what happens inside style.py afterward — only a live function call can.
Call set_theme()/toggle_theme() to change the active palette; any code
that reads a color via one of these functions AFTER that point sees the
new theme immediately. Widgets that bake a color into a stylesheet
string once at construction time still need to re-call these functions
and re-apply the result when the theme changes — see each widget's own
apply_theme()/_apply_theme() method (main_window.py, connection_screen
needs none — it has no per-widget color overrides, see below —
trajectory_screen.py, status_indicator.py, numeric_keypad.py,
manual_joystick.py, calibration_map_window.py, limit_violation_dialog.py
is transient and always reads fresh).
"""

# ---------------------------------------------------------------------------
# Sizing / spacing / typography — theme-independent, unchanged by a
# light/dark switch.
# ---------------------------------------------------------------------------

# Minimum touch target size. Apple/Google HIG guidelines recommend
# ~44-48px minimum; we use a larger value since this is an industrial/
# medical-style interface operated with less precision than a phone.
TOUCH_BUTTON_MIN_HEIGHT = 70
TOUCH_BUTTON_MIN_WIDTH = 120

# Spacing between interactive elements, to avoid accidental taps on
# adjacent controls.
LAYOUT_SPACING = 16
LAYOUT_MARGIN = 20

# Font sizes (points). Kept large for at-a-glance readability from a
# normal standing/working distance from the simulator.
FONT_SIZE_NORMAL = 16
FONT_SIZE_LARGE = 20
FONT_SIZE_STATUS = 24

# Stylesheet fragment applied to all primary action buttons. Sizing
# only (no color properties) — the actual colors come from
# APP_STYLESHEET's generic QPushButton rule, which DOES change with the
# theme (see APP_STYLESHEET() below), so this stays a plain constant.
BUTTON_STYLE = f"""
    QPushButton {{
        min-height: {TOUCH_BUTTON_MIN_HEIGHT}px;
        min-width: {TOUCH_BUTTON_MIN_WIDTH}px;
        font-size: {FONT_SIZE_LARGE}px;
        border-radius: 8px;
        padding: 8px;
    }}
"""

# Smaller variant for screens with many secondary/frequent-tap controls
# (e.g. per-axis manual movement, position load/save) where the full
# BUTTON_STYLE size makes the layout feel cramped. Still respects the
# ~44-48px HIG touch-target minimum noted above — only the generous
# extra margin used by primary actions (Connect, Home, Run) is trimmed.
COMPACT_BUTTON_MIN_HEIGHT = 48
COMPACT_BUTTON_MIN_WIDTH = 80

BUTTON_STYLE_COMPACT = f"""
    QPushButton {{
        min-height: {COMPACT_BUTTON_MIN_HEIGHT}px;
        min-width: {COMPACT_BUTTON_MIN_WIDTH}px;
        font-size: {FONT_SIZE_NORMAL}px;
        border-radius: 6px;
        padding: 4px;
    }}
"""

# Slimmer still — for a panel where every pixel of height is precious
# (trajectory_screen.py's sidebar, 2026-07-25+ layout passes, per Luis's
# own explicit request): the live plot needs the room more than any
# button around it does. Deliberately below the ~44-48px HIG floor
# noted above — a conscious tradeoff for this one dense secondary
# control cluster sitting next to a dominant visualization, accepted
# because it's still a real, if smaller, touch target (not removed),
# and Luis asked for exactly this.
SLIM_BUTTON_MIN_HEIGHT = 28
SLIM_BUTTON_MIN_WIDTH = 80

BUTTON_STYLE_SLIM = f"""
    QPushButton {{
        min-height: {SLIM_BUTTON_MIN_HEIGHT}px;
        min-width: {SLIM_BUTTON_MIN_WIDTH}px;
        font-size: {FONT_SIZE_NORMAL}px;
        border-radius: 6px;
        padding: 2px;
    }}
"""

# Diameter (px) of the ESP32 status indicator ("foquito") in the nav bar.
STATUS_INDICATOR_DIAMETER = 26

FONT_SIZE_TITLE = 52      # welcome_screen.py wordmark
FONT_SIZE_SUBTITLE = 15   # welcome_screen.py tagline
FONT_SIZE_HINT = 13       # welcome_screen.py tap-to-continue hint

FONT_FAMILY_CONDENSED = "DejaVu Sans Condensed"

APP_NAME = "GAITSIM"
APP_TAGLINE = "Control de Simulador de Marcha"

# Unifies two previously-inconsistent, unexplained ad hoc values
# (ConnectionScreen's saved-position combo/inputs used 44px min-height,
# TrajectoryScreen's trajectory combo used 60px) into the one touch
# target size already used for COMPACT_BUTTON_MIN_HEIGHT elsewhere.
TOUCH_INPUT_MIN_HEIGHT = 48

# Sizing only, no color — colors come from APP_STYLESHEET()'s QLineEdit/
# QComboBox rule.
INPUT_STYLE = f"""
    QLineEdit, QComboBox {{
        font-size: {FONT_SIZE_NORMAL}px;
        min-height: {TOUCH_INPUT_MIN_HEIGHT}px;
    }}
"""

NAV_BUTTON_HEIGHT = 34
EXIT_BUTTON_WIDTH = 56
JOYSTICK_BUTTON_SIZE = 40

# ---------------------------------------------------------------------------
# Theme palettes — raw hex values only, keyed by semantic name. The dark
# one is the original "instrument panel" look (unchanged values); the
# light one is a first-pass companion tuned for the same contrast roles
# (text on surface, white text on accent/danger/status chips) but, unlike
# the dark palette, has not been independently run through the dataviz
# skill's contrast checks — treat it as a solid starting point, not a
# certified-accessible palette.
# ---------------------------------------------------------------------------

_DARK = {
    "bg": "#16181c",
    "surface": "#212429",
    "surface_alt": "#2a2e35",
    "border": "#33373f",
    "text": "#e8e8e6",
    "text_muted": "#8a8d93",
    "accent": "#3987e5",
    "accent_hover": "#2f76c9",
    "accent_pressed": "#2861a8",
    "accent_text": "#ffffff",
    "danger": "#e74c3c",
    "danger_hover": "#c0392b",
    "danger_pressed": "#a5281b",
    "axis_x": "#3987e5",
    "axis_y": "#008300",
    "axis_angle": "#d55181",
    "platform_fill": "#e5c53f",
    "platform_underside": "#b89a2f",
    "indicator_ring": "#00000040",
    "status_disconnected": "#e74c3c",
    "status_connected": "#3987e5",
    "status_idle": "#2ecc71",
    "status_homing": "#f39c12",
    "status_receiving_trajectory": "#f39c12",
    "status_running": "#3987e5",
    "status_paused": "#e5c53f",
    "status_error": "#e74c3c",
}

_LIGHT = {
    "bg": "#f3f4f6",
    "surface": "#ffffff",
    "surface_alt": "#eceef1",
    "border": "#d3d7dd",
    "text": "#1b1e22",
    "text_muted": "#5b6169",
    "accent": "#1f6fd1",
    "accent_hover": "#1a5cae",
    "accent_pressed": "#154a8c",
    "accent_text": "#ffffff",
    "danger": "#d33a2c",
    "danger_hover": "#b53024",
    "danger_pressed": "#94271d",
    "axis_x": "#1f6fd1",
    "axis_y": "#0a6e0a",
    "axis_angle": "#b83c68",
    "platform_fill": "#c99a2e",
    "platform_underside": "#9c7a20",
    "indicator_ring": "#00000030",
    "status_disconnected": "#d33a2c",
    "status_connected": "#1f6fd1",
    "status_idle": "#1f9d4c",
    "status_homing": "#c9790c",
    "status_receiving_trajectory": "#c9790c",
    "status_running": "#1f6fd1",
    "status_paused": "#a9790a",
    "status_error": "#d33a2c",
}

_PALETTES = {"dark": _DARK, "light": _LIGHT}
_active_theme = "dark"


def get_theme_name() -> str:
    return _active_theme


def set_theme(name: str) -> None:
    if name not in _PALETTES:
        raise ValueError(f"Unknown theme '{name}'; expected one of {list(_PALETTES)}")
    global _active_theme
    _active_theme = name


def toggle_theme() -> str:
    """Flips dark<->light and returns the new theme name."""
    set_theme("light" if _active_theme == "dark" else "dark")
    return _active_theme


def _c(key: str) -> str:
    return _PALETTES[_active_theme][key]


# ---------------------------------------------------------------------------
# Color "constants" — each a zero-arg function (see module docstring for
# why), named to read like a constant at the call site: COLOR_BG() etc.
# ---------------------------------------------------------------------------

def COLOR_BG(): return _c("bg")
def COLOR_SURFACE(): return _c("surface")
def COLOR_SURFACE_ALT(): return _c("surface_alt")
def COLOR_BORDER(): return _c("border")
def COLOR_TEXT(): return _c("text")
def COLOR_TEXT_MUTED(): return _c("text_muted")
def COLOR_ACCENT(): return _c("accent")
def COLOR_ACCENT_HOVER(): return _c("accent_hover")
def COLOR_ACCENT_PRESSED(): return _c("accent_pressed")
def COLOR_ACCENT_TEXT(): return _c("accent_text")
def COLOR_DANGER(): return _c("danger")
def COLOR_DANGER_HOVER(): return _c("danger_hover")
def COLOR_DANGER_PRESSED(): return _c("danger_pressed")
def COLOR_AXIS_X(): return _c("axis_x")
def COLOR_AXIS_Y(): return _c("axis_y")
def COLOR_AXIS_ANGLE(): return _c("axis_angle")
def COLOR_PLATFORM_FILL(): return _c("platform_fill")
def COLOR_PLATFORM_UNDERSIDE(): return _c("platform_underside")
def COLOR_INDICATOR_RING(): return _c("indicator_ring")


def STATUS_COLORS() -> dict:
    """One color per SystemState name, plus 'ERROR' for a transient
    device-reported error — same keys system_state.py's SystemState
    members use, so callers can index this directly by state name."""
    p = _PALETTES[_active_theme]
    return {
        "DISCONNECTED": p["status_disconnected"],
        "CONNECTED": p["status_connected"],
        "IDLE": p["status_idle"],
        "HOMING": p["status_homing"],
        "RECEIVING_TRAJECTORY": p["status_receiving_trajectory"],
        "RUNNING": p["status_running"],
        "PAUSED": p["status_paused"],
        "ERROR": p["status_error"],
    }


# ---------------------------------------------------------------------------
# Color-bearing stylesheet fragments — also functions, for the same
# reason. Call with () at every use site.
# ---------------------------------------------------------------------------

def BUTTON_STYLE_PRIMARY() -> str:
    """Primary-action variant of BUTTON_STYLE (accent-filled) — for the
    one clear "main" action per screen (Run, Aceptar), so it stands out
    from the secondary actions around it."""
    return f"""
        QPushButton {{
            min-height: {TOUCH_BUTTON_MIN_HEIGHT}px;
            min-width: {TOUCH_BUTTON_MIN_WIDTH}px;
            font-size: {FONT_SIZE_LARGE}px;
            font-weight: 600;
            border-radius: 8px;
            padding: 8px;
            background-color: {COLOR_ACCENT()};
            color: {COLOR_ACCENT_TEXT()};
            border: 1px solid {COLOR_ACCENT()};
        }}
        QPushButton:hover {{
            background-color: {COLOR_ACCENT_HOVER()};
        }}
        QPushButton:pressed {{
            background-color: {COLOR_ACCENT_PRESSED()};
            border: 1px solid {COLOR_ACCENT_PRESSED()};
        }}
        QPushButton:disabled {{
            background-color: {COLOR_SURFACE_ALT()};
            color: {COLOR_TEXT_MUTED()};
            border: 1px solid {COLOR_BORDER()};
        }}
    """


def BUTTON_STYLE_PRIMARY_COMPACT() -> str:
    """Compact-height counterpart to BUTTON_STYLE_PRIMARY()."""
    return f"""
        QPushButton {{
            min-height: {COMPACT_BUTTON_MIN_HEIGHT}px;
            min-width: {COMPACT_BUTTON_MIN_WIDTH}px;
            font-size: {FONT_SIZE_NORMAL}px;
            font-weight: 600;
            border-radius: 6px;
            padding: 4px;
            background-color: {COLOR_ACCENT()};
            color: {COLOR_ACCENT_TEXT()};
            border: 1px solid {COLOR_ACCENT()};
        }}
        QPushButton:hover {{
            background-color: {COLOR_ACCENT_HOVER()};
        }}
        QPushButton:pressed {{
            background-color: {COLOR_ACCENT_PRESSED()};
            border: 1px solid {COLOR_ACCENT_PRESSED()};
        }}
        QPushButton:disabled {{
            background-color: {COLOR_SURFACE_ALT()};
            color: {COLOR_TEXT_MUTED()};
            border: 1px solid {COLOR_BORDER()};
        }}
    """


def BUTTON_STYLE_PRIMARY_SLIM() -> str:
    """SLIM-height counterpart to BUTTON_STYLE_PRIMARY() — used by Run."""
    return f"""
        QPushButton {{
            min-height: {SLIM_BUTTON_MIN_HEIGHT}px;
            min-width: {SLIM_BUTTON_MIN_WIDTH}px;
            font-size: {FONT_SIZE_NORMAL}px;
            font-weight: 600;
            border-radius: 6px;
            padding: 2px;
            background-color: {COLOR_ACCENT()};
            color: {COLOR_ACCENT_TEXT()};
            border: 1px solid {COLOR_ACCENT()};
        }}
        QPushButton:hover {{
            background-color: {COLOR_ACCENT_HOVER()};
        }}
        QPushButton:pressed {{
            background-color: {COLOR_ACCENT_PRESSED()};
            border: 1px solid {COLOR_ACCENT_PRESSED()};
        }}
        QPushButton:disabled {{
            background-color: {COLOR_SURFACE_ALT()};
            color: {COLOR_TEXT_MUTED()};
            border: 1px solid {COLOR_BORDER()};
        }}
    """


def NAV_BUTTON_STYLE() -> str:
    """Checkable "segmented control" style for the top nav bar, so the
    active screen is visually indicated."""
    return f"""
        QPushButton {{
            min-height: {NAV_BUTTON_HEIGHT}px;
            min-width: 140px;
            font-size: {FONT_SIZE_NORMAL}px;
            font-weight: 600;
            border-radius: 6px;
            padding: 4px 14px;
            background-color: transparent;
            color: {COLOR_TEXT_MUTED()};
            border: 1px solid transparent;
        }}
        QPushButton:checked {{
            background-color: {COLOR_SURFACE()};
            color: {COLOR_TEXT()};
            border: 1px solid {COLOR_ACCENT()};
        }}
        QPushButton:hover {{
            color: {COLOR_TEXT()};
        }}
        QPushButton:pressed {{
            background-color: {COLOR_SURFACE_ALT()};
            border: 1px solid {COLOR_BORDER()};
        }}
    """


def CONNECTION_STATUS_BUTTON_STYLE() -> str:
    """The unified ESP32 connect/status button (status_indicator.py) —
    same pill height as the nav bar's own buttons, filled with the
    current state's STATUS_COLORS() entry instead of the checked/
    unchecked chrome NAV_BUTTON_STYLE() uses. {color} is filled in
    per-state by the widget itself via .replace()."""
    return f"""
        QPushButton {{
            min-height: {NAV_BUTTON_HEIGHT}px;
            min-width: 120px;
            font-size: {FONT_SIZE_NORMAL}px;
            font-weight: 600;
            border-radius: 6px;
            padding: 4px 14px;
            background-color: {{color}};
            color: {COLOR_ACCENT_TEXT()};
            border: 1px solid {{color}};
        }}
    """


def THEME_TOGGLE_BUTTON_STYLE() -> str:
    """The light/dark toggle button itself (main_window.py) — same pill
    footprint as its nav bar neighbors, neutral chrome (it's a mode
    switch, not a state indicator or a primary action)."""
    return f"""
        QPushButton {{
            min-height: {NAV_BUTTON_HEIGHT}px;
            max-height: {NAV_BUTTON_HEIGHT}px;
            min-width: {NAV_BUTTON_HEIGHT}px;
            max-width: {NAV_BUTTON_HEIGHT}px;
            font-size: {FONT_SIZE_LARGE}px;
            border-radius: 6px;
            padding: 0px;
            background-color: {COLOR_SURFACE_ALT()};
            color: {COLOR_TEXT()};
            border: 1px solid {COLOR_BORDER()};
        }}
        QPushButton:hover {{
            border: 1px solid {COLOR_ACCENT()};
        }}
        QPushButton:pressed {{
            background-color: {COLOR_SURFACE()};
        }}
    """


def EXIT_BUTTON_STYLE() -> str:
    """Exit button, living in the nav bar's own row. Solid red at rest
    (not just on hover) per explicit request — reuses COLOR_DANGER()
    (one red, one meaning, app-wide) with a progressively darker shade
    on hover/press."""
    return f"""
        QPushButton {{
            min-height: {NAV_BUTTON_HEIGHT}px;
            max-height: {NAV_BUTTON_HEIGHT}px;
            min-width: {EXIT_BUTTON_WIDTH}px;
            max-width: {EXIT_BUTTON_WIDTH}px;
            font-size: {FONT_SIZE_LARGE}px;
            font-weight: 700;
            border-radius: 6px;
            padding: 0px;
            background-color: {COLOR_DANGER()};
            color: {COLOR_ACCENT_TEXT()};
            border: 1px solid {COLOR_DANGER()};
        }}
        QPushButton:hover {{
            background-color: {COLOR_DANGER_HOVER()};
            border: 1px solid {COLOR_DANGER_HOVER()};
        }}
        QPushButton:pressed {{
            background-color: {COLOR_DANGER_PRESSED()};
            border: 1px solid {COLOR_DANGER_PRESSED()};
        }}
    """


def JOYSTICK_BUTTON_STYLE() -> str:
    """Manual-move joystick directional buttons (manual_joystick.py)."""
    return f"""
        QPushButton {{
            min-height: {JOYSTICK_BUTTON_SIZE}px;
            max-height: {JOYSTICK_BUTTON_SIZE}px;
            min-width: {JOYSTICK_BUTTON_SIZE}px;
            max-width: {JOYSTICK_BUTTON_SIZE}px;
            font-size: {FONT_SIZE_LARGE}px;
            font-weight: 600;
            border-radius: 8px;
            padding: 0px;
        }}
        QPushButton:disabled {{
            color: {COLOR_TEXT_MUTED()};
        }}
    """


def JOYSTICK_CENTER_BUTTON_STYLE() -> str:
    """The joystick's central step-cycling number (1 -> 5 -> 10 -> 1) —
    accent-colored and round."""
    return f"""
        QPushButton {{
            min-height: {JOYSTICK_BUTTON_SIZE}px;
            max-height: {JOYSTICK_BUTTON_SIZE}px;
            min-width: {JOYSTICK_BUTTON_SIZE}px;
            max-width: {JOYSTICK_BUTTON_SIZE}px;
            font-size: {FONT_SIZE_NORMAL}px;
            font-weight: 700;
            border-radius: {JOYSTICK_BUTTON_SIZE // 2}px;
            padding: 0px;
            background-color: {COLOR_ACCENT()};
            color: {COLOR_ACCENT_TEXT()};
            border: 1px solid {COLOR_ACCENT()};
        }}
        QPushButton:hover {{
            background-color: {COLOR_ACCENT_HOVER()};
        }}
        QPushButton:pressed {{
            background-color: {COLOR_ACCENT_PRESSED()};
        }}
    """


def JOYSTICK_PANEL_STYLE() -> str:
    """Card container behind the whole joystick control (objectName-
    scoped, see manual_joystick.py)."""
    return f"""
        QWidget#manualJoystick {{
            background-color: {COLOR_SURFACE()};
            border: 1px solid {COLOR_BORDER()};
            border-radius: 10px;
        }}
    """


def APP_STYLESHEET() -> str:
    """
    Applied at the QApplication level (see main.py and
    theme_manager.py) — covers every widget that doesn't get its own
    setStyleSheet() call (QGroupBox cards, text inputs, dialogs...).
    BUTTON_STYLE/BUTTON_STYLE_COMPACT/BUTTON_STYLE_SLIM only set sizing,
    so those buttons inherit their colors from the QPushButton rule
    here — Qt's per-widget stylesheet only overrides properties it
    declares, not the whole selector.
    """
    return f"""
        QWidget {{
            background-color: {COLOR_BG()};
            color: {COLOR_TEXT()};
            font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif;
        }}

        QGroupBox {{
            background-color: {COLOR_SURFACE()};
            border: 1px solid {COLOR_BORDER()};
            border-radius: 10px;
            margin-top: 22px;
            padding-top: {LAYOUT_SPACING}px;
            font-size: {FONT_SIZE_NORMAL}px;
            font-weight: 600;
        }}

        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 14px;
            top: 4px;
            padding: 0 6px;
            color: {COLOR_TEXT_MUTED()};
        }}

        QLabel {{
            background: transparent;
        }}

        QLineEdit, QComboBox {{
            background-color: {COLOR_SURFACE_ALT()};
            border: 1px solid {COLOR_BORDER()};
            border-radius: 6px;
            padding: 6px 10px;
            color: {COLOR_TEXT()};
            selection-background-color: {COLOR_ACCENT()};
        }}

        QLineEdit:focus, QComboBox:focus {{
            border: 1px solid {COLOR_ACCENT()};
        }}

        QComboBox QAbstractItemView {{
            background-color: {COLOR_SURFACE_ALT()};
            color: {COLOR_TEXT()};
            selection-background-color: {COLOR_ACCENT()};
            border: 1px solid {COLOR_BORDER()};
        }}

        QPushButton {{
            background-color: {COLOR_SURFACE_ALT()};
            color: {COLOR_TEXT()};
            border: 1px solid {COLOR_BORDER()};
        }}

        QPushButton:hover {{
            border: 1px solid {COLOR_ACCENT()};
        }}

        QPushButton:pressed {{
            background-color: {COLOR_SURFACE()};
            border: 1px solid {COLOR_ACCENT()};
        }}

        QPushButton:disabled {{
            color: {COLOR_TEXT_MUTED()};
            background-color: {COLOR_SURFACE()};
            border: 1px solid {COLOR_BORDER()};
        }}

        QDialog, QMessageBox {{
            background-color: {COLOR_SURFACE()};
            color: {COLOR_TEXT()};
        }}

        QToolTip {{
            background-color: {COLOR_SURFACE_ALT()};
            color: {COLOR_TEXT()};
            border: 1px solid {COLOR_BORDER()};
        }}

        #headerBar {{
            background-color: {COLOR_SURFACE()};
            border-bottom: 1px solid {COLOR_BORDER()};
        }}
    """
