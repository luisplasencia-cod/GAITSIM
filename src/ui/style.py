"""
style.py

Centralized visual constants for touch-friendly UI design, sized for
the project's 10.1" touchscreen (1280x800 effective resolution).

Change values here to adjust sizing across the entire application
without touching individual screens.
"""

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

# Stylesheet fragment applied to all primary action buttons.
# Centralized here so visual identity (colors, corner radius) stays
# consistent across every screen without repeating strings everywhere.
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

# One color per SystemState name (see src/controllers/system_state.py),
# plus "ERROR" for a transient device-reported error. Centralized here
# so the status indicator and any future status display share one
# palette instead of repeating hex codes.
STATUS_COLORS = {
    "DISCONNECTED": "#e74c3c",
    "CONNECTED": "#3987e5",
    "IDLE": "#2ecc71",
    "HOMING": "#f39c12",
    "RECEIVING_TRAJECTORY": "#f39c12",
    "RUNNING": "#3987e5",
    "PAUSED": "#e5c53f",
    "ERROR": "#e74c3c",
}

# ---------------------------------------------------------------------------
# "Instrument panel" theme — dark lab/medical-equipment look, chosen to match
# the live trajectory plot's existing dark background (trajectory_screen.py)
# instead of leaving it as the one dark island in an otherwise default-gray
# Qt app. Applied app-wide via APP_STYLESHEET (see main.py), so individual
# screens don't repeat hex codes.
# ---------------------------------------------------------------------------
COLOR_BG = "#16181c"           # app background
COLOR_SURFACE = "#212429"      # card / panel surface (QGroupBox)
COLOR_SURFACE_ALT = "#2a2e35"  # inputs, header bar, hovered/checked chrome
COLOR_BORDER = "#33373f"
COLOR_TEXT = "#e8e8e6"
COLOR_TEXT_MUTED = "#8a8d93"
# Reuses STATUS_COLORS["CONNECTED"]/["RUNNING"] rather than introducing a
# second blue — one accent, one meaning.
COLOR_ACCENT = STATUS_COLORS["CONNECTED"]
COLOR_ACCENT_HOVER = "#2f76c9"
# One step darker than COLOR_ACCENT_HOVER — same progressive-darkening
# pattern (normal -> hover -> pressed), for the app's one touch-critical
# gap: no button anywhere had a :pressed state before the 2026-07-25+
# design-consistency pass, even though this is a touch-only kiosk where
# hover never actually happens — press IS the only feedback a tap gets.
COLOR_ACCENT_PRESSED = "#2861a8"
COLOR_ACCENT_TEXT = "#ffffff"

# One color per position axis (X/Y/angle), shared by the live trajectory
# plot and the position monitor (both in trajectory_screen.py — see
# src/ui/platform_view.py) so the same axis always reads as the same
# color everywhere in the app.
# Validated for a dark surface per the dataviz skill: fixed categorical
# order (blue/green/magenta), CVD/contrast checks pass on COLOR_BG.
COLOR_AXIS_X = "#3987e5"
COLOR_AXIS_Y = "#008300"
COLOR_AXIS_ANGLE = "#d55181"

# Used exclusively by welcome_screen.py (verified: no other screen
# references these) — kept here anyway per this file's own stated
# purpose ("change values here... without touching individual screens"),
# not moved locally, even though usage is currently single-screen.
FONT_SIZE_TITLE = 52      # wordmark — 2026-07-25 redesign, was 48. Verified
                          # to fit the welcome screen's 440px left column
                          # (with 6px letter-spacing this would clip; see
                          # welcome_screen.py's wordmark_font setup)
FONT_SIZE_SUBTITLE = 15   # tagline (now uppercase/tracked), was 18
FONT_SIZE_HINT = 13       # tap-to-continue hint label, new in the redesign

# Same condensed face as the welcome screen's wordmark (verified there to
# render as a genuinely distinct, narrower family in Qt, not a disguised
# fallback) — reused for the persistent nav bar's brand label so the same
# "instrument datasheet" identity shows up every time the brand appears,
# not just on the splash screen.
FONT_FAMILY_CONDENSED = "DejaVu Sans Condensed"

APP_NAME = "GAITSIM"
APP_TAGLINE = "Control de Simulador de Marcha"

# Unifies two previously-inconsistent, unexplained ad hoc values
# (ConnectionScreen's saved-position combo/inputs used 44px min-height,
# TrajectoryScreen's trajectory combo used 60px) into the one touch
# target size already used for COMPACT_BUTTON_MIN_HEIGHT elsewhere.
TOUCH_INPUT_MIN_HEIGHT = 48

INPUT_STYLE = f"""
    QLineEdit, QComboBox {{
        font-size: {FONT_SIZE_NORMAL}px;
        min-height: {TOUCH_INPUT_MIN_HEIGHT}px;
    }}
"""

# Named instead of left as bare literals scattered across files — same
# rendered colors, just one place to find/change them from.
COLOR_INDICATOR_RING = "#00000040"       # StatusIndicator's ring border
# NOTE: equals STATUS_COLORS["PAUSED"] by coincidence, not by shared
# meaning — kept as its own constant so a future PAUSED color change
# doesn't silently recolor the platform drawing too, and vice versa.
COLOR_PLATFORM_FILL = "#e5c53f"          # Monitor Posición platform fill
COLOR_PLATFORM_UNDERSIDE = "#b89a2f"     # Monitor Posición platform underside

# Primary-action variant of BUTTON_STYLE (accent-filled) — for the one
# clear "main" action per screen (Conectar, Run), so it stands out from
# the secondary actions around it instead of every button looking equally
# important.
BUTTON_STYLE_PRIMARY = f"""
    QPushButton {{
        min-height: {TOUCH_BUTTON_MIN_HEIGHT}px;
        min-width: {TOUCH_BUTTON_MIN_WIDTH}px;
        font-size: {FONT_SIZE_LARGE}px;
        font-weight: 600;
        border-radius: 8px;
        padding: 8px;
        background-color: {COLOR_ACCENT};
        color: {COLOR_ACCENT_TEXT};
        border: 1px solid {COLOR_ACCENT};
    }}
    QPushButton:hover {{
        background-color: {COLOR_ACCENT_HOVER};
    }}
    QPushButton:pressed {{
        background-color: {COLOR_ACCENT_PRESSED};
        border: 1px solid {COLOR_ACCENT_PRESSED};
    }}
    QPushButton:disabled {{
        background-color: {COLOR_SURFACE_ALT};
        color: {COLOR_TEXT_MUTED};
        border: 1px solid {COLOR_BORDER};
    }}
"""

# Compact-height counterpart to BUTTON_STYLE_PRIMARY — same accent
# treatment (still the one "main" action), sized down for a tight
# panel (trajectory_screen.py's sidebar, 2026-07-25 layout pass) where
# the full touch height would crowd out the small live plot below it.
BUTTON_STYLE_PRIMARY_COMPACT = f"""
    QPushButton {{
        min-height: {COMPACT_BUTTON_MIN_HEIGHT}px;
        min-width: {COMPACT_BUTTON_MIN_WIDTH}px;
        font-size: {FONT_SIZE_NORMAL}px;
        font-weight: 600;
        border-radius: 6px;
        padding: 4px;
        background-color: {COLOR_ACCENT};
        color: {COLOR_ACCENT_TEXT};
        border: 1px solid {COLOR_ACCENT};
    }}
    QPushButton:hover {{
        background-color: {COLOR_ACCENT_HOVER};
    }}
    QPushButton:pressed {{
        background-color: {COLOR_ACCENT_PRESSED};
        border: 1px solid {COLOR_ACCENT_PRESSED};
    }}
    QPushButton:disabled {{
        background-color: {COLOR_SURFACE_ALT};
        color: {COLOR_TEXT_MUTED};
        border: 1px solid {COLOR_BORDER};
    }}
"""

# SLIM-height counterpart to BUTTON_STYLE_PRIMARY(_COMPACT) — for Run
# specifically once it lives in a panel dense enough to need
# BUTTON_STYLE_SLIM's height (trajectory_screen.py's sidebar, 2026-07-25+
# passes). Still the one accent-colored "main action" button, just as
# short as everything else around it now.
BUTTON_STYLE_PRIMARY_SLIM = f"""
    QPushButton {{
        min-height: {SLIM_BUTTON_MIN_HEIGHT}px;
        min-width: {SLIM_BUTTON_MIN_WIDTH}px;
        font-size: {FONT_SIZE_NORMAL}px;
        font-weight: 600;
        border-radius: 6px;
        padding: 2px;
        background-color: {COLOR_ACCENT};
        color: {COLOR_ACCENT_TEXT};
        border: 1px solid {COLOR_ACCENT};
    }}
    QPushButton:hover {{
        background-color: {COLOR_ACCENT_HOVER};
    }}
    QPushButton:pressed {{
        background-color: {COLOR_ACCENT_PRESSED};
        border: 1px solid {COLOR_ACCENT_PRESSED};
    }}
    QPushButton:disabled {{
        background-color: {COLOR_SURFACE_ALT};
        color: {COLOR_TEXT_MUTED};
        border: 1px solid {COLOR_BORDER};
    }}
"""

# Checkable "segmented control" style for the top nav bar, so the active
# screen is visually indicated (previously nav buttons gave no feedback
# about which screen was current).
NAV_BUTTON_STYLE = f"""
    QPushButton {{
        min-height: 52px;
        min-width: 160px;
        font-size: {FONT_SIZE_NORMAL}px;
        font-weight: 600;
        border-radius: 8px;
        padding: 6px 18px;
        background-color: transparent;
        color: {COLOR_TEXT_MUTED};
        border: 1px solid transparent;
    }}
    QPushButton:checked {{
        background-color: {COLOR_SURFACE};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_ACCENT};
    }}
    QPushButton:hover {{
        color: {COLOR_TEXT};
    }}
    QPushButton:pressed {{
        background-color: {COLOR_SURFACE_ALT};
        border: 1px solid {COLOR_BORDER};
    }}
"""

# Applied once at the QApplication level (see main.py) — covers every
# widget that doesn't get its own setStyleSheet() call (QGroupBox cards,
# text inputs, dialogs...). BUTTON_STYLE/BUTTON_STYLE_COMPACT above only
# set sizing, so buttons inherit their colors from the QPushButton rule
# here — Qt's per-widget stylesheet only overrides properties it declares,
# not the whole selector.
APP_STYLESHEET = f"""
    QWidget {{
        background-color: {COLOR_BG};
        color: {COLOR_TEXT};
        font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif;
    }}

    QGroupBox {{
        background-color: {COLOR_SURFACE};
        border: 1px solid {COLOR_BORDER};
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
        color: {COLOR_TEXT_MUTED};
    }}

    QLabel {{
        background: transparent;
    }}

    QLineEdit, QComboBox {{
        background-color: {COLOR_SURFACE_ALT};
        border: 1px solid {COLOR_BORDER};
        border-radius: 6px;
        padding: 6px 10px;
        color: {COLOR_TEXT};
        selection-background-color: {COLOR_ACCENT};
    }}

    QLineEdit:focus, QComboBox:focus {{
        border: 1px solid {COLOR_ACCENT};
    }}

    QComboBox QAbstractItemView {{
        background-color: {COLOR_SURFACE_ALT};
        color: {COLOR_TEXT};
        selection-background-color: {COLOR_ACCENT};
        border: 1px solid {COLOR_BORDER};
    }}

    QPushButton {{
        background-color: {COLOR_SURFACE_ALT};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_BORDER};
    }}

    QPushButton:hover {{
        border: 1px solid {COLOR_ACCENT};
    }}

    QPushButton:pressed {{
        background-color: {COLOR_SURFACE};
        border: 1px solid {COLOR_ACCENT};
    }}

    QPushButton:disabled {{
        color: {COLOR_TEXT_MUTED};
        background-color: {COLOR_SURFACE};
        border: 1px solid {COLOR_BORDER};
    }}

    QDialog, QMessageBox {{
        background-color: {COLOR_SURFACE};
        color: {COLOR_TEXT};
    }}

    QToolTip {{
        background-color: {COLOR_SURFACE_ALT};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_BORDER};
    }}

    #headerBar {{
        background-color: {COLOR_SURFACE};
        border-bottom: 1px solid {COLOR_BORDER};
    }}
"""