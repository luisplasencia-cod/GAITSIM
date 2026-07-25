"""
main_window.py

Top-level application window. Owns the single shared
ESP32Controller / SystemStateMachine / StateMachineBridge instances and
hands them to each screen. Uses a QStackedWidget to switch between
screens without destroying their state (e.g. the active serial
connection must survive navigation).
"""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QPushButton, QLabel, QButtonGroup
)

from src.communication.esp32_controller import ESP32Controller
from src.controllers.initial_position_session import InitialPositionSession
from src.controllers.system_state import SystemStateMachine
from src.ui.bridge import StateMachineBridge
from src.ui.screens.connection_screen import ConnectionScreen
from src.ui.screens.trajectory_screen import TrajectoryScreen
from src.ui.screens.welcome_screen import WelcomeScreen
from src.ui.calibration_map_window import CalibrationMapWindow
from src.ui.status_indicator import StatusIndicator
from src.ui.style import (
    NAV_BUTTON_STYLE, FONT_SIZE_NORMAL, LAYOUT_SPACING, LAYOUT_MARGIN,
    FONT_FAMILY_CONDENSED,
)


class MainWindow(QMainWindow):
    """
    Application entry window.

    Args:
        serial_port: Path to the ESP32's serial device, e.g.
                     "/dev/ttyUSB0".
    """

    def __init__(self, serial_port: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gait Simulator Control")

        controller = ESP32Controller(port=serial_port)
        state_machine = SystemStateMachine(controller)
        self._bridge = StateMachineBridge(state_machine)
        self._position_session = InitialPositionSession()

        self._connection_screen = ConnectionScreen(self._bridge, self._position_session)
        # TrajectoryScreen is now this app's main working view: it
        # consolidates what used to be 3 separate windows (Monitor
        # Posición, Trayectorias, Live Trajectory Plot) — see that
        # screen's module docstring.
        self._trajectory_screen = TrajectoryScreen(self._bridge, self._position_session)
        self._trajectory_screen.request_new_trial.connect(self._show_connection_screen)
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
            self._calibration_map_window = CalibrationMapWindow(self._bridge)
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

    def _build_nav_bar(self) -> QWidget:
        """
        Branded header bar: app name on the left, screen navigation as a
        checkable segmented control in the middle (so the active screen
        is visually indicated — plain buttons gave no such feedback),
        and the ESP32 status indicator pinned to the right so it stays
        visible regardless of which screen is active.
        """
        bar = QWidget()
        bar.setObjectName("headerBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(
            LAYOUT_MARGIN, LAYOUT_MARGIN // 2, LAYOUT_MARGIN, LAYOUT_MARGIN // 2
        )
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

        self._connection_nav_btn = QPushButton("Connection / Manual")
        self._connection_nav_btn.setCheckable(True)
        self._connection_nav_btn.setChecked(True)
        self._connection_nav_btn.setStyleSheet(NAV_BUTTON_STYLE)
        self._connection_nav_btn.clicked.connect(lambda: self._stack.setCurrentIndex(0))

        trajectory_btn = QPushButton("Trajectories")
        trajectory_btn.setCheckable(True)
        trajectory_btn.setStyleSheet(NAV_BUTTON_STYLE)
        trajectory_btn.clicked.connect(lambda: self._stack.setCurrentIndex(1))

        self._nav_group.addButton(self._connection_nav_btn)
        self._nav_group.addButton(trajectory_btn)

        layout.addWidget(self._connection_nav_btn)
        layout.addWidget(trajectory_btn)
        layout.addStretch()

        calibration_map_btn = QPushButton("Espacio Disponible")
        calibration_map_btn.setStyleSheet(NAV_BUTTON_STYLE)
        calibration_map_btn.clicked.connect(self._open_calibration_map)
        layout.addWidget(calibration_map_btn)
        layout.addSpacing(LAYOUT_SPACING)

        status_label = QLabel("ESP32:")
        status_label.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px;")
        layout.addWidget(status_label)
        layout.addWidget(StatusIndicator(self._bridge))

        return bar

    def closeEvent(self, event):
        """Ensure the serial connection is cleanly closed on exit."""
        controller = self._bridge.state_machine.controller
        if controller.is_connected:
            controller.disconnect()
        super().closeEvent(event)