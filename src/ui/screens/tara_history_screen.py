"""
tara_history_screen.py

Read-only "hoja de cálculo" view over every tara (basal / no-contact
reference) and contact-test ("prueba") recorded via TrajectoryScreen's
Tara button and Run-with-detach flow (see src/controllers/tara_library.py
and SystemStateMachine.perform_pre_run_detach()). One row per prueba, so
Luis can see every contact-threshold test he's run across every ensayo,
with an on-demand export to .xlsx for post-analysis.

Purely file-based (reads tara_library.py directly) — no bridge/state
machine dependency, same "screens know nothing of each other" rule as
every other screen, since this one doesn't need live device state at
all.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox,
)

from src.controllers import tara_library
from src.ui.theme_manager import ThemeManager
from src.ui.style import (
    BUTTON_STYLE_SLIM, BUTTON_STYLE_PRIMARY_SLIM,
    LAYOUT_SPACING, LAYOUT_MARGIN, FONT_SIZE_NORMAL, FONT_SIZE_LARGE,
    COLOR_SURFACE, COLOR_SURFACE_ALT, COLOR_BORDER, COLOR_TEXT,
    COLOR_TEXT_MUTED, COLOR_ACCENT, COLOR_ACCENT_TEXT,
)

_COLUMNS = (
    "Ensayo", "Tara Y (cm)", "Tara Á (°)", "Fecha Tara",
    "Y Prueba (cm)", "Fecha Prueba",
)


class TaraHistoryScreen(QWidget):
    """
    Args:
        theme_manager: Shared ThemeManager — re-applies the table's own
            baked-in colors on a live theme toggle, same pattern as
            every other screen (see style.py's module docstring).
    """

    def __init__(self, theme_manager: ThemeManager, parent=None):
        super().__init__(parent)
        self._theme_manager = theme_manager
        self._build_ui()
        self._theme_manager.theme_changed.connect(self._apply_theme)
        self._apply_theme(self._theme_manager.name)
        self._refresh()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(LAYOUT_SPACING)
        outer.setContentsMargins(
            LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN
        )

        title = QLabel("HISTORIAL DE PRUEBAS DE CONTACTO")
        title_font = title.font()
        title_font.setBold(True)
        title_font.setPointSize(FONT_SIZE_LARGE)
        title.setFont(title_font)
        outer.addWidget(title)

        subtitle = QLabel(
            "Una fila por prueba de contacto ejecutada (Run) con una tara "
            "registrada. La tara sin pruebas ejecutadas aún aparece con "
            "las columnas de prueba en blanco."
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

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        outer.addWidget(self.table, stretch=1)

        self.info_label = QLabel("")
        self.info_label.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px;")
        outer.addWidget(self.info_label)

    def showEvent(self, event):
        super().showEvent(event)
        # A tara/prueba recorded on TrajectoryScreen while this screen
        # wasn't visible wouldn't otherwise show up until an explicit
        # "Actualizar" — refresh every time this screen is switched to,
        # same spirit as TrajectoryScreen's own showEvent refresh.
        self._refresh()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _rows(self):
        """
        Flattens every TaraRecord into one row per prueba (Ensayo, Tara
        Y/Á/fecha repeated, Y/fecha de esa prueba puntual) — or a single
        row with blank prueba columns for a tara with none yet, so it's
        still visible in the table.
        """
        rows = []
        for record in tara_library.load_all():
            base = (
                record.trajectory_id,
                f"{record.tara.y:.2f}",
                f"{record.tara.angle:.1f}",
                record.tara_timestamp,
            )
            if not record.pruebas:
                rows.append(base + ("", ""))
            else:
                for prueba in record.pruebas:
                    rows.append(base + (f"{prueba.y:.2f}", prueba.timestamp))
        return rows

    def _refresh(self):
        try:
            rows = self._rows()
        except Exception as exc:
            self.info_label.setText(f"No se pudo leer el historial: {exc}")
            rows = []

        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row_index, col_index, item)

        self.info_label.setText(
            f"{len(rows)} prueba(s) registrada(s)."
            if rows else "Sin pruebas registradas todavía."
        )

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export_clicked(self):
        rows = self._rows()
        if not rows:
            QMessageBox.information(
                self, "Exportar", "No hay datos para exportar todavía."
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar Historial de Pruebas",
            "historial_pruebas_contacto.xlsx",
            "Excel (*.xlsx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"

        try:
            # Deferred import: pandas/openpyxl are only needed for this
            # on-demand export, not for anything else this screen (or
            # the rest of the app) does.
            import pandas as pd
            pd.DataFrame(rows, columns=_COLUMNS).to_excel(
                path, index=False, engine="openpyxl"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Exportar", f"No se pudo exportar: {exc}")
            return

        self.info_label.setText(f"Historial exportado a '{path}'.")

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _apply_theme(self, _name: str) -> None:
        self.export_button.setStyleSheet(BUTTON_STYLE_PRIMARY_SLIM())
        self.table.setStyleSheet(f"""
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
                background-color: {COLOR_ACCENT()};
                color: {COLOR_ACCENT_TEXT()};
            }}
            QHeaderView::section {{
                background-color: {COLOR_SURFACE_ALT()};
                color: {COLOR_TEXT()};
                padding: 6px;
                border: 1px solid {COLOR_BORDER()};
                font-weight: bold;
            }}
        """)
