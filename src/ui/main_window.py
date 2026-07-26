"""
main_window.py

Top-level application window. Owns the single shared
ESP32Controller / SystemStateMachine / StateMachineBridge instances and
hands them to each screen. Uses a QStackedWidget to switch between
screens without destroying their state (e.g. the active serial
connection must survive navigation).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QPushButton, QLabel, QButtonGroup, QMessageBox
)

from src.communication.esp32_controller import ESP32Controller
from src.controllers.initial_position_session import InitialPositionSession
from src.controllers.system_state import SystemStateMachine
from src.ui.bridge import StateMachineBridge
from src.ui.screens.connection_screen import ConnectionScreen
from src.ui.screens.trajectory_screen import TrajectoryScreen
from src.ui.screens.welcome_screen import WelcomeScreen
from src.ui.calibration_map_window import CalibrationMapWindow
from src.ui.status_indicator import ConnectionStatusButton
from src.ui.theme_manager import ThemeManager
from src.ui.style import (
    NAV_BUTTON_STYLE, EXIT_BUTTON_STYLE, THEME_TOGGLE_BUTTON_STYLE,
    FONT_SIZE_NORMAL, LAYOUT_SPACING, LAYOUT_MARGIN, FONT_FAMILY_CONDENSED,
)


class MainWindow(QMainWindow):
    """
    Application entry window.

    Args:
        serial_port: Path to the ESP32's serial device, e.g.
                     "/dev/ttyUSB0".
        theme_manager: Shared ThemeManager (see src/ui/theme_manager.py),
                     created once in main.py — passed down to every
                     screen/widget that bakes a color into its own
                     stylesheet, so each can re-apply it live when the
                     operator toggles light/dark (see the nav bar button
                     built in _build_nav_bar).
    """

    def __init__(self, serial_port: str, theme_manager: ThemeManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gait Simulator Control")
        # Kiosk mode (2026-07-25+ request): no title bar / OS close button,
        # on top of always launching fullscreen (see main.py) — the "X"
        # button built below is the only way out, gated by closeEvent's
        # confirmation.
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)

        self._theme_manager = theme_manager

        controller = ESP32Controller(port=serial_port)
        state_machine = SystemStateMachine(controller)
        self._bridge = StateMachineBridge(state_machine)
        self._position_session = InitialPositionSession()

        self._connection_screen = ConnectionScreen(
            self._bridge, self._position_session, theme_manager
        )
        # TrajectoryScreen is now this app's main working view: it
        # consolidates what used to be 3 separate windows (Monitor
        # Posición, Trayectorias, Live Trajectory Plot) — see that
        # screen's module docstring.
        self._trajectory_screen = TrajectoryScreen(
            self._bridge, self._position_session, theme_manager
        )
        self._trajectory_screen.request_new_trial.connect(self._show_connection_screen)
        # 2026-07-26+, Luis's explicit request: once "Ir a Posición
        # Inicial" (Inicio tab) actually produces a real movement, jump
        # to the Monitor tab so the operator lands where the platform
        # view/joystick live — see ConnectionScreen.request_show_monitor's
        # own docstring for exactly which cases count as "a real move".
        self._connection_screen.request_show_monitor.connect(self._show_monitor_screen)
        self._calibration_map_window = None  # created lazily, see _open_calibration_map

        self._stack = QStackedWidget()
        self._stack.addWidget(self._connection_screen)   # index 0
        self._stack.addWidget(self._trajectory_screen)    # index 1

        nav_bar = self._build_nav_bar()

        app_shell = QWidget()
        shell_layout = QVBoxLayout(app_shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(nav_bar)
        shell_layout.addWidget(self._stack)

        # Root stack: idle/welcome screen first (see welcome_screen.py for
        # why — branding on startup, tap-to-enter attract screen once the
        # rig is powered but not in use), then the functional app shell.
        self._welcome_screen = WelcomeScreen()
        self._welcome_screen.continue_requested.connect(self._enter_app)

        self._root_stack = QStackedWidget()
        self._root_stack.addWidget(self._welcome_screen)  # index 0
        self._root_stack.addWidget(app_shell)              # index 1
        self.setCentralWidget(self._root_stack)

    def closeEvent(self, event):
        """
        Always confirm before actually closing, regardless of what
        triggered the close request (the exit button, Alt+F4, a window
        manager close) — this is the single place that gate lives, per
        the "controlled exit" requirement (2026-07-25+ kiosk pass).
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Salir")
        box.setText("¿Está seguro que desea salir?")
        yes_button = box.addButton("Sí", QMessageBox.AcceptRole)
        box.addButton("No", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not yes_button:
            event.ignore()
            return

        controller = self._bridge.state_machine.controller
        if controller.is_connected:
            controller.disconnect()
        event.accept()

    def _enter_app(self):
        self._root_stack.setCurrentWidget(self._root_stack.widget(1))

    def _open_calibration_map(self):
        """
        Opens the (single, lazily-created) calibration-space-map window,
        or raises it to the front if already open — non-modal, same
        pattern as _open_monitor_3d. Moved out of ConnectionScreen (see
        calibration_map_window.py) so the diagram has real room to read.
        """
        if self._calibration_map_window is None:
            self._calibration_map_window = CalibrationMapWindow(self._bridge, self._theme_manager)
        self._calibration_map_window.show()
        self._calibration_map_window.raise_()
        self._calibration_map_window.activateWindow()

    def _show_connection_screen(self):
        """
        Triggered by TrajectoryScreen.request_new_trial ("Elegir Otro
        Ensayo") — switches screens AND updates the nav bar's checked
        state, since that only happens automatically when the operator
        clicks the nav button directly, not on a programmatic switch
        like this one.
        """
        self._stack.setCurrentIndex(0)
        self._connection_nav_btn.setChecked(True)

    def _show_monitor_screen(self):
        """
        Triggered by ConnectionScreen.request_show_monitor, once "Ir a
        Posición Inicial" actually produces a real movement — see that
        signal's docstring for which cases count. Same nav-bar-checked-
        state bookkeeping as _show_connection_screen, mirrored.
        """
        self._stack.setCurrentIndex(1)
        self._trajectory_nav_btn.setChecked(True)

    def _build_nav_bar(self) -> QWidget:
        """
        Branded header bar: app name on the left, screen navigation as a
        checkable segmented control in the middle (so the active screen
        is visually indicated — plain buttons gave no such feedback),
        and the unified ESP32 connect/status button pinned to the right
        so it stays visible regardless of which screen is active.

        Vertical margins trimmed (2026-07-25+ kiosk pass, Luis's explicit
        request) so this persistent chrome strip takes less of the
        screen, leaving more room for the actual content below it — see
        NAV_BUTTON_STYLE's own height trim in style.py for the other half
        of that change.
        """
        bar = QWidget()
        bar.setObjectName("headerBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(LAYOUT_MARGIN, 6, LAYOUT_MARGIN, 6)
        layout.setSpacing(LAYOUT_SPACING)

        brand_label = QLabel("GAITSIM")
        # Same condensed face + tracking mechanism as welcome_screen.py's
        # wordmark (via QFont.setLetterSpacing, not QSS — verified that
        # QSS's letter-spacing property is a silent no-op in this Qt
        # version), so the brand reads consistently everywhere it
        # appears, not just on the splash screen. Font size unchanged
        # from before (16px) — condensed is narrower than the previous
        # plain face at the same size, so this can only reduce any
        # clipping risk in the nav bar, never increase it.
        brand_font = QFont(FONT_FAMILY_CONDENSED)
        brand_font.setBold(True)
        brand_font.setPointSize(FONT_SIZE_NORMAL)
        brand_font.setLetterSpacing(QFont.AbsoluteSpacing, 2)
        brand_label.setFont(brand_font)
        layout.addWidget(brand_label)
        layout.addSpacing(LAYOUT_SPACING)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        self._connection_nav_btn = QPushButton("Inicio")
        self._connection_nav_btn.setCheckable(True)
        self._connection_nav_btn.setChecked(True)
        self._connection_nav_btn.setStyleSheet(NAV_BUTTON_STYLE())
        self._connection_nav_btn.clicked.connect(lambda: self._stack.setCurrentIndex(0))

        self._trajectory_nav_btn = QPushButton("Monitor")
        self._trajectory_nav_btn.setCheckable(True)
        self._trajectory_nav_btn.setStyleSheet(NAV_BUTTON_STYLE())
        self._trajectory_nav_btn.clicked.connect(lambda: self._stack.setCurrentIndex(1))

        self._nav_group.addButton(self._connection_nav_btn)
        self._nav_group.addButton(self._trajectory_nav_btn)

        layout.addWidget(self._connection_nav_btn)
        layout.addWidget(self._trajectory_nav_btn)
        layout.addStretch()

        self._calibration_map_btn = QPushButton("Espacio Disponible")
        self._calibration_map_btn.setStyleSheet(NAV_BUTTON_STYLE())
        self._calibration_map_btn.clicked.connect(self._open_calibration_map)
        layout.addWidget(self._calibration_map_btn)
        layout.addSpacing(LAYOUT_SPACING)

        # Light/dark theme toggle (2026-07-26+, Luis's explicit
        # request) — to the LEFT of the connect/status button, per that
        # request. Shows the icon of whichever mode is CURRENTLY active;
        # tapping it switches to the other one, live (see _apply_theme
        # and theme_manager.py — every screen re-applies its own colors
        # on the same signal this connects to).
        self._theme_toggle_btn = QPushButton()
        self._theme_toggle_btn.setStyleSheet(THEME_TOGGLE_BUTTON_STYLE())
        self._theme_toggle_btn.clicked.connect(self._theme_manager.toggle)
        layout.addWidget(self._theme_toggle_btn)
        layout.addSpacing(LAYOUT_SPACING)

        # Unified connect/status control (2026-07-25+ kiosk pass) —
        # replaces the former separate "ESP32:" label + colored
        # indicator pair; see src/ui/status_indicator.py.
        self._connection_status_btn = ConnectionStatusButton(self._bridge, self._theme_manager)
        layout.addWidget(self._connection_status_btn)
        layout.addSpacing(LAYOUT_SPACING)

        # Exit button: the only way to close the app (see closeEvent,
        # the single place its confirmation dialog lives, gating this
        # AND any other close trigger like Alt+F4). Lives in the nav
        # bar's own row — as a normal layout item it's automatically
        # vertically centered and never overlaps the connect/status
        # button next to it, unlike an earlier floating-corner version.
        self._exit_button = QPushButton("✕")
        self._exit_button.setStyleSheet(EXIT_BUTTON_STYLE())
        self._exit_button.setToolTip("Salir")
        self._exit_button.clicked.connect(self.close)
        layout.addWidget(self._exit_button)

        self._theme_manager.theme_changed.connect(self._apply_theme)
        self._apply_theme(self._theme_manager.name)

        return bar

    def _apply_theme(self, theme_name: str) -> None:
        """
        Re-applies every color-bearing stylesheet THIS window owns
        directly (the nav bar's own buttons) — ConnectionStatusButton
        re-colors itself (see status_indicator.py), and the two screens
        do the same for their own widgets (see each screen's own
        _apply_theme). Also called once at nav-bar build time so the
        toggle button's icon/tooltip start correct without waiting for
        a first toggle.
        """
        for button in (
            self._connection_nav_btn, self._trajectory_nav_btn,
            self._calibration_map_btn,
        ):
            button.setStyleSheet(NAV_BUTTON_STYLE())
        self._exit_button.setStyleSheet(EXIT_BUTTON_STYLE())
        self._theme_toggle_btn.setStyleSheet(THEME_TOGGLE_BUTTON_STYLE())
        if theme_name == "dark":
            self._theme_toggle_btn.setText("🌙")
            self._theme_toggle_btn.setToolTip("Cambiar a modo claro")
        else:
            self._theme_toggle_btn.setText("☀")
            self._theme_toggle_btn.setToolTip("Cambiar a modo oscuro")