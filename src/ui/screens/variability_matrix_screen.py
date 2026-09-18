"""
variability_matrix_screen.py

Height-variability / repeatability test matrix — replaces the old
"Pruebas" screen (tara_history_screen.py's flat, un-contextualized
list of contact tests). Columns are "talla" (subject/prosthesis
height, cm — parsed from each ensayo CSV's filename via
trajectory_library.parse_talla_cm(), fixed range 162-180cm in 2cm
steps per Luis's spec, 2026-09-18), rows are fixed depths below that
ensayo's recorded tara (see tara_library.py): the tara itself (0mm)
plus 5mm below it, one row per millimeter. Each cell tracks up to
SAMPLES_PER_CELL repeated executions of that exact (talla, depth)
point, so Luis always knows exactly which point a result belongs to
instead of an undifferentiated list.

Selecting a cell shows its recorded samples and an "Ejecutar punto"
button that MOVES the platform to that (talla, depth) point (see
SystemStateMachine.go_to_variability_point()) — the same action
re-visits a specific saved point on demand, satisfying "poder
seleccionar esa fila/columna y volver a ejecutar el ensayo guardado
para ese punto".

IMPORTANT (corrected 2026-09-18, same day as the first version):
"Ejecutar punto" only REPOSITIONS the platform — it does not run the
ensayo and does not by itself count as a sample. Luis's explicit
correction: the repeatability counter must increase when the ENSAYO's
own Run (pressed separately, via TrajectoryScreen, once already
loaded/sent for this talla) actually FINISHES, not when the
repositioning move completes. This screen tracks that as a "pending
sample" (`self._pending_sample`) set right after a successful
"Ejecutar punto", consumed by the shared bridge's `trajectory_finished`
signal (or `device_error`/a pause-then-abort observed via
`state_changed`) — see _on_trajectory_finished/_on_device_error/
_on_state_changed below. Known limitation: like ConnectionScreen's own
`_awaiting_initial_move` guard (the same pattern), this trusts that the
very next trajectory to finish while a sample is pending IS the
intended Run — an unrelated trajectory finishing first (e.g. the
operator doing something else on another screen in between) would be
wrongly attributed. Not solved further; matches the risk already
accepted by that existing pattern elsewhere in this app.

Needs the shared bridge (unlike the old read-only tara_history_screen)
because moving to a point is a real blocking device action — same
_ActionWorker-on-background-thread pattern as TrajectoryScreen/
ConnectionScreen (see src/ui/action_worker.py) — and because tracking
the pending sample needs the bridge's trajectory_finished/device_error/
state_changed signals.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox,
)

from src.controllers import tara_library, variability_library
from src.controllers.variability_library import DEPTH_ROWS_MM, SAMPLES_PER_CELL
from src.ui.action_worker import ActionWorker as _ActionWorker
from src.ui.bridge import StateMachineBridge
from src.ui.device_error_dialog import show_device_error
from src.ui.theme_manager import ThemeManager
from src.ui.style import (
    BUTTON_STYLE_SLIM, BUTTON_STYLE_PRIMARY_SLIM,
    LAYOUT_SPACING, LAYOUT_MARGIN, FONT_SIZE_NORMAL, FONT_SIZE_LARGE,
    COLOR_SURFACE, COLOR_SURFACE_ALT, COLOR_BORDER, COLOR_TEXT,
    COLOR_TEXT_MUTED, COLOR_ACCENT, COLOR_ACCENT_TEXT, COLOR_DANGER,
)
from src.utils import trajectory_library

# Fixed matrix column range — Luis's spec (2026-09-18): 10 tallas from
# 162 to 180cm in 2cm steps. Shown even when no CSV exists yet for a
# given talla (per Luis: CSVs will be added later, just follow the
# "talla<N>" filename convention already used).
TALLA_MIN_CM = 162
TALLA_MAX_CM = 180
TALLA_STEP_CM = 2
TALLAS_CM = list(range(TALLA_MIN_CM, TALLA_MAX_CM + 1, TALLA_STEP_CM))

_SAMPLE_COLUMNS = ("#", "Y real (cm)", "Éxito", "Fecha", "")


class VariabilityMatrixScreen(QWidget):
    """
    Args:
        bridge: Shared StateMachineBridge — moving to a matrix point is
            a real device action (see SystemStateMachine.
            go_to_variability_point()), unlike the old read-only
            tara_history_screen — and the subsequent ensayo Run that
            actually completes a sample is tracked via this same
            bridge's signals (see module docstring, "pending sample").
        theme_manager: Shared ThemeManager, same pattern as every other
            screen.
    """

    def __init__(self, bridge: StateMachineBridge, theme_manager: ThemeManager, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._theme_manager = theme_manager
        self._worker = None
        # (talla_cm, depth_mm) of the currently selected cell, or None.
        self._selected = None
        # talla_cm -> trajectory_id, rebuilt on every refresh (a CSV
        # may be added/removed between visits).
        self._column_trajectory_id = {}
        # Set right after a successful "Ejecutar punto" (a plain dict:
        # trajectory_id/depth_mm/y_real/interrupted), consumed by the
        # NEXT trajectory_finished/device_error — see module docstring.
        # None whenever no point-move is awaiting its Run.
        self._pending_sample = None

        self._build_ui()
        self._bridge.state_changed.connect(self._on_state_changed)
        self._bridge.trajectory_finished.connect(self._on_trajectory_finished)
        self._bridge.device_error.connect(self._on_device_error)
        self._bridge.disconnected.connect(self._on_disconnected)
        self._theme_manager.theme_changed.connect(self._apply_theme)
        self._apply_theme(self._theme_manager.name)
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        # A CSV or a sample recorded elsewhere while this screen wasn't
        # visible wouldn't otherwise show up until an explicit
        # "Actualizar" — same refresh-on-show as every other screen.
        self._refresh()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(LAYOUT_SPACING)
        outer.setContentsMargins(
            LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN
        )

        title = QLabel("MATRIZ DE VARIABILIDAD Y REPETIBILIDAD")
        title_font = title.font()
        title_font.setBold(True)
        title_font.setPointSize(FONT_SIZE_LARGE)
        title.setFont(title_font)
        outer.addWidget(title)

        subtitle = QLabel(
            "Columnas = talla del ensayo (162-180cm). Filas = profundidad "
            "respecto a la tara registrada (0mm = tara, -1 a -5mm = "
            f"debajo). Cada celda admite hasta {SAMPLES_PER_CELL} muestras "
            "de repetibilidad. Selecciona una celda, presiona \"Ejecutar "
            "punto\" para MOVER el rig a ese punto, luego ve a Monitor y "
            "presiona Run — la muestra se registra cuando ese ensayo "
            "termina, no al llegar al punto."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT_MUTED()};"
        )
        outer.addWidget(subtitle)

        button_row = QHBoxLayout()
        self.refresh_button = QPushButton("Actualizar")
        self.refresh_button.setStyleSheet(BUTTON_STYLE_SLIM)
        self.refresh_button.clicked.connect(self._refresh)
        self.export_button = QPushButton("Exportar a Excel (.xlsx)")
        self.export_button.setStyleSheet(BUTTON_STYLE_PRIMARY_SLIM())
        self.export_button.clicked.connect(self._on_export_clicked)
        button_row.addWidget(self.refresh_button)
        button_row.addWidget(self.export_button)
        button_row.addStretch()
        outer.addLayout(button_row)

        content_row = QHBoxLayout()
        content_row.setSpacing(LAYOUT_SPACING)

        self.table = QTableWidget(len(DEPTH_ROWS_MM), len(TALLAS_CM))
        self.table.setHorizontalHeaderLabels([f"{cm} cm" for cm in TALLAS_CM])
        self.table.setVerticalHeaderLabels(
            ["Tara (0mm)" if mm == 0 else f"{mm}mm" for mm in DEPTH_ROWS_MM]
        )
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectItems)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.currentCellChanged.connect(self._on_cell_selected)
        content_row.addWidget(self.table, stretch=2)

        detail_box = QGroupBox("PUNTO SELECCIONADO")
        detail_layout = QVBoxLayout(detail_box)
        self.detail_label = QLabel("Selecciona una celda de la matriz.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px;")
        detail_layout.addWidget(self.detail_label)

        self.samples_table = QTableWidget(0, len(_SAMPLE_COLUMNS))
        self.samples_table.setHorizontalHeaderLabels(_SAMPLE_COLUMNS)
        self.samples_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.samples_table.verticalHeader().setVisible(False)
        self.samples_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        detail_layout.addWidget(self.samples_table, stretch=1)

        self.execute_button = QPushButton("Ejecutar punto")
        self.execute_button.setStyleSheet(BUTTON_STYLE_PRIMARY_SLIM())
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self._on_execute_clicked)
        detail_layout.addWidget(self.execute_button)

        content_row.addWidget(detail_box, stretch=1)
        outer.addLayout(content_row, stretch=1)

        self.info_label = QLabel("")
        self.info_label.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px;")
        self.info_label.setWordWrap(True)
        outer.addWidget(self.info_label)

    # ------------------------------------------------------------------
    # Data / grid refresh
    # ------------------------------------------------------------------

    def _trajectory_id_for_talla(self, talla_cm: int):
        """
        First CSV (sorted, for determinism) whose parsed talla matches
        `talla_cm`, or None if no CSV covers this talla yet — expected
        for most of the 10 columns until Luis uploads the corresponding
        ensayos (per his explicit note: current CSVs are just examples
        of the naming convention, not the definitive set).
        """
        matches = sorted(
            tid for tid in trajectory_library.list_trajectories()
            if trajectory_library.parse_talla_cm(tid) == talla_cm
        )
        return matches[0] if matches else None

    def _refresh(self):
        self._column_trajectory_id = {
            talla: self._trajectory_id_for_talla(talla) for talla in TALLAS_CM
        }
        for col, talla in enumerate(TALLAS_CM):
            trajectory_id = self._column_trajectory_id[talla]
            record = (
                variability_library.load_matrix(trajectory_id)
                if trajectory_id is not None else None
            )
            has_tara = (
                trajectory_id is not None
                and tara_library.has_tara(trajectory_id)
            )
            for row, depth_mm in enumerate(DEPTH_ROWS_MM):
                item = QTableWidgetItem()
                item.setTextAlignment(Qt.AlignCenter)
                if trajectory_id is None:
                    item.setText("—")
                    item.setToolTip("Sin CSV para esta talla todavía.")
                elif not has_tara:
                    item.setText("—")
                    item.setToolTip("Sin tara registrada para este ensayo.")
                else:
                    samples = record.rows[depth_mm]
                    item.setText(f"{len(samples)}/{SAMPLES_PER_CELL}")
                item.setBackground(self._cell_color(
                    record.rows[depth_mm] if record is not None else None
                ))
                self.table.setItem(row, col, item)
        self._refresh_detail()
        self._refresh_execute_button()

    def _cell_color(self, samples) -> QColor:
        if not samples:
            return QColor(COLOR_SURFACE_ALT())
        if any(not s.success for s in samples):
            return QColor(COLOR_DANGER())
        if len(samples) >= SAMPLES_PER_CELL:
            return QColor(COLOR_ACCENT())
        return QColor(COLOR_SURFACE())

    def _on_cell_selected(self, row, col, _prev_row, _prev_col):
        if row < 0 or col < 0:
            self._selected = None
        else:
            self._selected = (TALLAS_CM[col], DEPTH_ROWS_MM[row])
        self._refresh_detail()
        self._refresh_execute_button()

    def _refresh_detail(self):
        self.samples_table.setRowCount(0)
        if self._selected is None:
            self.detail_label.setText("Selecciona una celda de la matriz.")
            return

        talla, depth_mm = self._selected
        trajectory_id = self._column_trajectory_id.get(talla)
        depth_text = "tara (0mm)" if depth_mm == 0 else f"{depth_mm}mm bajo tara"
        if trajectory_id is None:
            self.detail_label.setText(
                f"Talla {talla}cm, {depth_text}: no hay ningún CSV "
                f"'talla{talla}...' cargado todavía."
            )
            return

        try:
            tara_record = tara_library.load_tara(trajectory_id)
        except tara_library.TaraNotFoundError:
            self.detail_label.setText(
                f"'{trajectory_id}' ({depth_text}): sin tara registrada — "
                "regístrala desde Trayectorias antes de ejecutar esta matriz."
            )
            return

        record = variability_library.load_matrix(trajectory_id)
        samples = record.rows[depth_mm]
        self.detail_label.setText(
            f"'{trajectory_id}' — {depth_text}\n"
            f"Tara: Y={tara_record.tara.y:.2f}cm  Á={tara_record.tara.angle:.1f}°\n"
            f"{len(samples)}/{SAMPLES_PER_CELL} muestra(s) registrada(s)."
        )
        self.samples_table.setRowCount(len(samples))
        for i, sample in enumerate(samples):
            values = (
                str(i + 1), f"{sample.y_real:.3f}",
                "Sí" if sample.success else "No", sample.timestamp,
            )
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignCenter)
                self.samples_table.setItem(i, c, item)
            # Per-row delete (2026-09-18, Luis's explicit request) — lets
            # a bad/mistaken sample be discarded without losing the rest
            # of this cell's data. Captures `i` by default arg (not
            # closure) since `i` is reused across loop iterations.
            delete_button = QPushButton("✕")
            delete_button.setToolTip("Eliminar esta muestra")
            delete_button.setStyleSheet(BUTTON_STYLE_SLIM)
            delete_button.clicked.connect(
                lambda _checked=False, index=i: self._on_delete_sample_clicked(index)
            )
            self.samples_table.setCellWidget(i, len(_SAMPLE_COLUMNS) - 1, delete_button)

    def _refresh_execute_button(self):
        sm = self._bridge.state_machine
        enabled = False
        if self._selected is not None and sm.can_go_to_position():
            talla, _depth_mm = self._selected
            trajectory_id = self._column_trajectory_id.get(talla)
            if trajectory_id is not None and tara_library.has_tara(trajectory_id):
                enabled = True
        self.execute_button.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Execute a point: "Ejecutar punto" only REPOSITIONS the platform —
    # see module docstring's 2026-09-18 correction. The sample itself
    # is recorded later, by _on_trajectory_finished/_on_device_error/
    # _on_state_changed reacting to the operator's SEPARATE Run.
    # ------------------------------------------------------------------

    def _on_execute_clicked(self):
        talla, depth_mm = self._selected
        trajectory_id = self._column_trajectory_id[talla]
        try:
            tara_record = tara_library.load_tara(trajectory_id)
        except tara_library.TaraNotFoundError:
            self.info_label.setText(f"'{trajectory_id}' no tiene tara registrada.")
            return

        sm = self._bridge.state_machine
        result_holder = {}

        def action():
            result_holder["position"] = sm.go_to_variability_point(
                tara_record.tara, depth_mm
            )

        self.execute_button.setEnabled(False)
        self.info_label.setText(
            f"Moviendo a '{trajectory_id}' a {depth_mm}mm de la tara..."
        )
        self._worker = _ActionWorker(action)
        self._worker.succeeded.connect(
            lambda: self._on_point_reached(trajectory_id, depth_mm, result_holder["position"])
        )
        self._worker.failed.connect(self._on_execute_failed)
        self._worker.start()

    def _on_point_reached(self, trajectory_id: str, depth_mm: int, position) -> None:
        # Overwrites any previous pending sample (e.g. the operator
        # picked a different cell and re-clicked "Ejecutar punto"
        # without ever pressing Run for the first one) — that earlier
        # attempt is simply abandoned, no sample recorded for it.
        self._pending_sample = {
            "trajectory_id": trajectory_id, "depth_mm": depth_mm,
            "y_real": position.y, "interrupted": False,
        }
        self.info_label.setText(
            f"En posición: '{trajectory_id}' a {depth_mm}mm de la tara "
            f"(Y={position.y:.3f}cm). Ve a Monitor y presiona Run para "
            f"registrar la muestra."
        )
        self._refresh()

    def _on_execute_failed(self, message: str):
        self.info_label.setText(f"No se pudo llegar al punto: {message}")
        self._refresh_execute_button()

    def _finalize_pending_sample(self, success: bool) -> None:
        """Records (or silently drops, if `success` is None) the
        pending sample and clears it — the single place every
        finalization path (FINISHED, ERROR, pause-then-abort) funnels
        through."""
        pending = self._pending_sample
        self._pending_sample = None
        if pending is None:
            return
        variability_library.add_sample(
            pending["trajectory_id"], pending["depth_mm"],
            pending["y_real"], success,
        )
        status = "OK" if success else "FALLIDA (interrumpida/pausada)"
        self.info_label.setText(
            f"Muestra registrada — '{pending['trajectory_id']}' a "
            f"{pending['depth_mm']}mm: {status}."
        )
        self._refresh()

    def _on_trajectory_finished(self):
        # Fires for ANY trajectory finishing anywhere in the app (see
        # module docstring's known-limitation note) — only acts when a
        # sample is actually pending, same guard pattern as
        # ConnectionScreen's own _awaiting_initial_move.
        if self._pending_sample is not None:
            self._finalize_pending_sample(success=not self._pending_sample["interrupted"])

    def _on_state_changed(self, state_name: str):
        if self._pending_sample is not None:
            if state_name == "PAUSED":
                self._pending_sample["interrupted"] = True
            elif state_name == "IDLE" and self._pending_sample["interrupted"]:
                # Paused, then aborted (PAUSED -> IDLE never fires
                # trajectory_finished — see abort()'s own docstring in
                # system_state.py) — finalize as failed now instead of
                # leaving a stale pending sample forever.
                self._finalize_pending_sample(success=False)
        self._refresh()

    def _on_device_error(self, code: str, message: str):
        if self._pending_sample is not None:
            self._finalize_pending_sample(success=False)
        show_device_error(self, "_device_error_dialog", self, code, message)

    def _on_disconnected(self):
        # A real disconnect mid-Run means nothing meaningful was
        # measured — drop the pending sample without recording it
        # (unlike a device error, which IS a real data point).
        self._pending_sample = None
        self._refresh()

    def _on_delete_sample_clicked(self, index: int) -> None:
        talla, depth_mm = self._selected
        trajectory_id = self._column_trajectory_id[talla]
        try:
            variability_library.delete_sample(trajectory_id, depth_mm, index)
        except variability_library.VariabilityLibraryError as exc:
            self.info_label.setText(f"No se pudo eliminar la muestra: {exc}")
            return
        self.info_label.setText(
            f"Muestra #{index + 1} eliminada de '{trajectory_id}' a {depth_mm}mm."
        )
        self._refresh()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export_clicked(self):
        columns = ("Talla (cm)", "Profundidad (mm)", "#", "Y real (cm)", "Éxito", "Fecha")
        rows = []
        for talla in TALLAS_CM:
            trajectory_id = self._column_trajectory_id.get(talla)
            if trajectory_id is None:
                continue
            record = variability_library.load_matrix(trajectory_id)
            for depth_mm in DEPTH_ROWS_MM:
                for i, sample in enumerate(record.rows[depth_mm]):
                    rows.append((
                        talla, depth_mm, i + 1, f"{sample.y_real:.3f}",
                        "Sí" if sample.success else "No", sample.timestamp,
                    ))

        if not rows:
            QMessageBox.information(
                self, "Exportar", "No hay datos para exportar todavía."
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar Matriz de Variabilidad",
            "matriz_variabilidad.xlsx", "Excel (*.xlsx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"

        try:
            # Deferred import: pandas/openpyxl only needed for this
            # on-demand export, same as tara_history_screen's own.
            import pandas as pd
            pd.DataFrame(rows, columns=columns).to_excel(
                path, index=False, engine="openpyxl"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Exportar", f"No se pudo exportar: {exc}")
            return

        self.info_label.setText(f"Matriz exportada a '{path}'.")

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _apply_theme(self, _name: str) -> None:
        self.export_button.setStyleSheet(BUTTON_STYLE_PRIMARY_SLIM())
        self.execute_button.setStyleSheet(BUTTON_STYLE_PRIMARY_SLIM())
        table_style = f"""
            QTableWidget {{
                background-color: {COLOR_SURFACE()};
                color: {COLOR_TEXT()};
                gridline-color: {COLOR_BORDER()};
                border: 1px solid {COLOR_BORDER()};
                font-size: {FONT_SIZE_NORMAL}px;
            }}
            QTableWidget::item {{
                padding: 6px;
            }}
            QTableWidget::item:selected {{
                border: 2px solid {COLOR_ACCENT()};
            }}
            QHeaderView::section {{
                background-color: {COLOR_SURFACE_ALT()};
                color: {COLOR_TEXT()};
                padding: 6px;
                border: 1px solid {COLOR_BORDER()};
                font-weight: bold;
            }}
        """
        self.table.setStyleSheet(table_style)
        self.samples_table.setStyleSheet(table_style)
        # Cell background colors are set directly on each item (they
        # encode success/failure/partial state, not selection), so
        # they must be redrawn after a theme change too.
        self._refresh()
