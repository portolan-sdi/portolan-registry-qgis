"""The SQL tab of "Add to map": the rows, the columns, and the SQL of a GeoParquet load.

A choice applies to the next load or download as soon as the user makes it.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsCheckableComboBox, QgsCodeEditorSQL, QgsExpressionLineEdit
from qgis.PyQt.QtCore import Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from portolan_registry_qgis.core import mirror, parquet_query
from portolan_registry_qgis.core.i18n import N_, _, count, ngettext, number
from portolan_registry_qgis.core.loadable import parquet_name
from portolan_registry_qgis.core.query import DEFAULT_QUERY, Query, QueryError, selection
from portolan_registry_qgis.gui.preview import preview_model
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.filter_expression import parse_filter
from portolan_registry_qgis.qgis_io.network import run_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from qgis.core import QgsTask

    from portolan_registry_qgis.core.geoparquet import ReadPlan
    from portolan_registry_qgis.core.query import Condition
    from portolan_registry_qgis.core.stac import Asset, Document

# The wait after the last key press before the tab checks the filter or the SQL, in milliseconds.
CHECK_DELAY_MS = 300
# How long Copy SQL says "Copied", in milliseconds.
COPIED_MS = 1500
# The lines of SQL the editor shows before it scrolls.
SQL_LINES = 10
# The rows the preview table shows before it scrolls.
PREVIEW_LINES = 9
# The memory provider's name for each field kind, for the expression builder.
_FIELD_TYPES = {
    "bool": "boolean",
    "int": "integer",
    "double": "double",
    "string": "string",
    "date": "date",
    "datetime": "datetime",
}
ROWS_TIP = N_(
    "Filter rows with a QGIS expression, such as \"type\" = 'school'. "
    "Leave blank to include all rows."
)
COLUMNS_TIP = N_("Select columns to include. Geometry is always included.")


def fields_layer(plan: ReadPlan) -> QgsVectorLayer:
    """Return a memory layer with the plan's columns, which the expression builder lists."""
    fields = "&".join(
        f"field={bytes(QUrl.toPercentEncoding(column.name)).decode()}:{_FIELD_TYPES[column.kind]}"
        for column in plan.columns
    )
    return QgsVectorLayer(f"None?{fields}", "columns", "memory")


def file_stem(name: str) -> str:
    """Return ``name`` as a file name without unsafe characters."""
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "data"


def parquet_assets(document: Document) -> list[Asset]:
    """Return the GeoParquet files of ``document`` that the SQL tab can query."""
    return [
        item.asset
        for item in document.loadable()
        if item.asset is not None and item.format == "parquet"
    ]


class ColumnsBox(QgsCheckableComboBox):
    """A dropdown of check boxes, one per column, that says how many are checked."""

    def __init__(self) -> None:
        super().__init__()
        # The base class writes the checked names after each change. The count replaces them later.
        # The model signals every change, also one that code makes.
        self.model().dataChanged.connect(lambda *_args: QTimer.singleShot(0, self.show_count))

    def show_count(self) -> None:
        """Write "2 of 14 columns" in the closed dropdown."""
        total = self.count()
        checked = len(self.checkedItems())
        text = ngettext("{kept} of {n} column", "{kept} of {n} columns", total)
        self.lineEdit().setText(text.format(kept=number(checked), n=number(total)))

    def resizeEvent(self, event: object) -> None:
        """Keep the count after the base class writes the checked names on a resize."""
        super().resizeEvent(event)
        QTimer.singleShot(0, self.show_count)


def _error_label() -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    label.hide()
    return label


class SqlTab(QWidget):
    """Filter the rows and pick the columns of a GeoParquet file, or write the SQL by hand."""

    # Emits when the query, or a problem with it, changes.
    changed = pyqtSignal()
    # Asks the window to list the files of the items in the area for this asset.
    files_needed = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._limits: Callable[[], parquet_layer.Limits | None] = lambda: None
        self._assets: list[Asset] = []
        # The read columns of each file, by href, and the files of the items, by href.
        self._planned: dict[str, parquet_layer.Planned] = {}
        self._listed: dict[str, list[str]] = {}
        self._reading: set[str] = set()
        self._condition: Condition | None = None
        self._preview_task: QgsTask | None = None
        # The expression builder lists the columns of this layer. It stays out of the project.
        self._fields: QgsVectorLayer | None = None
        self._check = QTimer(self)
        self._check.setSingleShot(True)
        self._check.setInterval(CHECK_DELAY_MS)
        self._check.timeout.connect(self.check_now)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.addLayout(self._form())
        self.status = _error_label()
        layout.addWidget(self.status)
        layout.addWidget(self._sql_box())
        layout.addLayout(self._buttons())
        self.table = QTableView()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(
            self.table.verticalHeader().defaultSectionSize() * PREVIEW_LINES
        )
        self.table.hide()
        layout.addWidget(self.table)
        self.footer = QLabel()
        self.footer.hide()
        layout.addWidget(self.footer)

    # ---------- layout ----------

    def _form(self) -> QFormLayout:
        form = QFormLayout()
        self.file = QComboBox()
        self.file.setToolTip(_("Choose the GeoParquet file to query."))
        self.file.currentIndexChanged.connect(lambda _index: self._file_chosen())
        form.addRow(self._label(_("File"), _("Choose the GeoParquet file to query.")), self.file)
        self.filter = QgsExpressionLineEdit()
        self.filter.setExpressionDialogTitle(_("Filter rows"))
        self.filter.setToolTip(_(ROWS_TIP))
        self.filter.expressionChanged.connect(lambda _text: self._check.start())
        builder = self.filter.findChild(QToolButton)
        if builder is not None:
            builder.setToolTip(_("Open the expression builder."))
        self.filter_error = _error_label()
        rows = QVBoxLayout()
        rows.setSpacing(2)
        rows.addWidget(self.filter)
        rows.addWidget(self.filter_error)
        form.addRow(self._label(_("Rows"), _(ROWS_TIP)), rows)
        self.columns = ColumnsBox()
        self.columns.setToolTip(_(COLUMNS_TIP))
        self.columns.model().dataChanged.connect(lambda *_args: self._check.start())
        form.addRow(self._label(_("Columns"), _(COLUMNS_TIP)), self.columns)
        return form

    @staticmethod
    def _label(text: str, tip: str) -> QLabel:
        label = QLabel(text)
        label.setToolTip(tip)
        return label

    def _sql_box(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.sql = QgsCodeEditorSQL()
        self.sql.setReadOnly(True)
        self.sql.setLineNumbersVisible(False)
        self.sql.setToolTip(_("View the DuckDB query used to load the layer."))
        self.sql.setFixedHeight(self.sql.fontMetrics().lineSpacing() * SQL_LINES)
        self.sql.textChanged.connect(self._sql_typed)
        layout.addWidget(self.sql)
        self.sql_error = _error_label()
        layout.addWidget(self.sql_error)
        self.own_sql = QCheckBox(_("Write my own SQL"))
        self.own_sql.setToolTip(
            _("Replace the row and column selections with a custom SELECT query.")
        )
        self.own_sql.toggled.connect(self._set_editing)
        layout.addWidget(self.own_sql)
        return box

    def _buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.preview = QPushButton(_("Preview rows"))
        self.preview.setToolTip(
            count(
                "Preview the first {n} matching row without loading the layer.",
                "Preview the first {n} matching rows without loading the layer.",
                parquet_query.PREVIEW_ROWS,
            )
        )
        self.preview.clicked.connect(self._start_preview)
        self.copy = QPushButton(_("Copy SQL"))
        self.copy.setToolTip(_("Copy a DuckDB command-line script to export the same layer."))
        self.copy.clicked.connect(self._copy_sql)
        row.addWidget(self.preview)
        row.addWidget(self.copy)
        row.addStretch(1)
        return row

    # ---------- what the tab shows ----------

    def set_limits(self, limits: Callable[[], parquet_layer.Limits | None]) -> None:
        """Read the area of interest from ``limits``, which returns None while it is not complete."""
        self._limits = limits

    def show_document(self, document: Document) -> None:
        """List the GeoParquet files of ``document``, and forget the choice for the last document."""
        self._stop_preview()
        self._assets = parquet_assets(document)
        self._planned.clear()
        self._listed.clear()
        self._reading.clear()
        self.file.blockSignals(True)
        self.file.clear()
        for asset in self._assets:
            self.file.addItem(parquet_name(asset), asset.href)
        self.file.blockSignals(False)
        self.own_sql.blockSignals(True)
        self.own_sql.setChecked(False)
        self.own_sql.blockSignals(False)
        self._clear_choice()

    def has_files(self) -> bool:
        """Return whether the open document has a GeoParquet file to query."""
        return bool(self._assets)

    def asset(self) -> Asset | None:
        """Return the file the tab queries."""
        index = self.file.currentIndex()
        return self._assets[index] if 0 <= index < len(self._assets) else None

    def opened(self) -> None:
        """Read the columns of the chosen file. Call when the tab shows."""
        asset = self.asset()
        if asset is None or asset.href in self._planned or asset.href in self._reading:
            self._show_state()
            return
        source = self._source(asset)
        if source is None:
            self.files_needed.emit(asset)
            self._show_status(_("Listing the files of the items in the area…"))
            return
        self._describe(asset, source)

    def _source(self, asset: Asset) -> parquet_query.Source | None:
        if asset.read_url is not None:
            return asset.read_url
        tables = mirror.split_files(self._listed.get(asset.href, []))[1]
        return tables or None

    def set_item_files(self, asset: Asset, hrefs: list[str]) -> None:
        """Read the columns of the GeoParquet files that the window listed for ``asset``."""
        self._listed[asset.href] = hrefs
        if not mirror.split_files(hrefs)[1]:
            self._show_status(_("No item in the area has a GeoParquet file."))
            return
        if asset is self.asset():
            self.opened()

    def scope_changed(self) -> None:
        """Show the SQL for the new area. The files listed for the old area are dropped."""
        for href in list(self._listed):
            del self._listed[href]
            self._planned.pop(href, None)
        self._show_sql()
        if self.isVisible():
            self.opened()

    def _file_chosen(self) -> None:
        self._stop_preview()
        self._clear_choice()
        if self.isVisible():
            self.opened()

    def _clear_choice(self) -> None:
        """Empty the filter and the columns of the chosen file."""
        self.filter.blockSignals(True)
        self.filter.setExpression("")
        self.filter.blockSignals(False)
        self._condition = None
        self._fill_columns()
        self.check_now()

    def clear(self) -> None:
        """Remove all row and column selections, and the custom SQL."""
        self.own_sql.setChecked(False)
        self._clear_choice()

    def _describe(self, asset: Asset, source: parquet_query.Source) -> None:
        href = asset.href
        self._reading.add(href)
        read = parquet_layer.describer(source, parquet_layer.extension_directory())

        def done(result: object, error: BaseException | None) -> None:
            self._reading.discard(href)
            if isinstance(result, parquet_layer.Planned):
                self._planned[href] = result
                if asset is self.asset():
                    self._fill_columns()
                    self.check_now()
                return
            if asset is self.asset():
                text = parquet_query.read_error(source, error)
                self._show_status(_("Could not read the columns: {error}").format(error=text))

        self._show_status(_("Reading the columns…"))
        run_task(_("Read the columns of {url}").format(url=source), read, done)

    def _plan(self) -> parquet_layer.Planned | None:
        asset = self.asset()
        return self._planned.get(asset.href) if asset is not None else None

    def _fill_columns(self) -> None:
        planned = self._plan()
        self.columns.blockSignals(True)
        self.columns.clear()
        if planned is not None:
            for column in planned.plan.columns:
                self.columns.addItemWithCheckState(column.name, Qt.CheckState.Checked, column.name)
        self.columns.blockSignals(False)
        self.columns.show_count()
        self._fields = fields_layer(planned.plan) if planned is not None else None
        self.filter.setLayer(self._fields)

    def _show_status(self, text: str | None) -> None:
        self.status.setText(text or "")
        self.status.setVisible(bool(text))

    def _show_state(self) -> None:
        ready = self._plan() is not None
        editing = self.own_sql.isChecked()
        for widget in (self.filter, self.columns):
            widget.setEnabled(ready and not editing)
        self.own_sql.setEnabled(ready)
        ok = ready and self.problem() is None
        self.preview.setEnabled(ok)
        self.copy.setEnabled(ok)
        if ready:
            self._show_status(None)

    # ---------- the query ----------

    def query(self) -> Query:
        """Return the query the tab shows for the chosen file."""
        if self.own_sql.isChecked():
            return Query(sql=self.sql.text())
        text = self.filter.expression().strip()
        return Query(text, self._condition if text else None, self._chosen_columns())

    def _chosen_columns(self) -> tuple[str, ...] | None:
        planned = self._plan()
        if planned is None:
            return None
        checked = set(self.columns.checkedItemsData())
        names = tuple(column.name for column in planned.plan.columns if column.name in checked)
        return None if len(names) == len(planned.plan.columns) else names

    def query_for(self, asset: Asset) -> Query:
        """Return the query for a load of ``asset``. Another file than the chosen one gets the default."""
        chosen = self.asset()
        if chosen is None or asset.href != chosen.href:
            return DEFAULT_QUERY
        return self.query()

    def plan(self) -> ReadPlan | None:
        """Return the columns of the chosen file, or None before they are read."""
        planned = self._plan()
        return planned.plan if planned is not None else None

    def problem(self) -> str | None:
        """Return why the query cannot run, or None."""
        label = self.sql_error if self.own_sql.isChecked() else self.filter_error
        return label.text() or None

    def check_now(self) -> None:
        """Check the filter or the SQL without the wait, and show the one error under its field."""
        editing = self.own_sql.isChecked()
        self._show_error(self.sql_error, self._sql_problem() if editing else None)
        self._show_error(self.filter_error, None if editing else self._filter_problem())
        # A preview of the old choice would mislead, so it goes.
        self._stop_preview()
        self.table.hide()
        self.footer.hide()
        self._show_state()
        self._show_sql()
        self.changed.emit()

    @staticmethod
    def _show_error(label: QLabel, text: str | None) -> None:
        label.setText(text or "")
        label.setVisible(bool(text))

    def _filter_problem(self) -> str | None:
        text = self.filter.expression().strip()
        planned = self._plan()
        try:
            self._condition = parse_filter(text) if text else None
            if planned is not None:
                selection(self.query(), planned.plan)
        except QueryError as error:
            self._condition = None
            return str(error)
        return None

    def _sql_problem(self) -> str | None:
        try:
            parquet_query.check_select(self.sql.text())
        except (QueryError, parquet_query.DuckDBMissingError) as error:
            return str(error)
        return None

    def rows(self) -> parquet_layer.Planned | None:
        """Return the plan of the rows the query keeps, or None while the area or columns are not ready."""
        planned, limits = self._plan(), self._limits()
        if planned is None or limits is None:
            return None
        context = QgsProject.instance().transformContext()
        try:
            clip = parquet_layer.clip_for(limits, planned.crs, context)
        except ValueError:
            return None
        return replace(planned, clip=clip, query=self.query())

    def _show_sql(self) -> None:
        if self.own_sql.isChecked():
            return
        rows = None if self.problem() is not None else self.rows()
        text = "" if rows is None else parquet_query.display_select(rows.url, rows.plan, rows.rows)
        self.sql.blockSignals(True)
        self.sql.setText(text)
        # At the start, the cursor marks no parenthesis.
        self.sql.setCursorPosition(0, 0)
        self.sql.blockSignals(False)

    # ---------- custom SQL ----------

    def _set_editing(self, editing: bool) -> None:  # noqa: FBT001 - the toggled(bool) signal gives the value by position
        self.sql.setReadOnly(not editing)
        self.check_now()
        if editing:
            self.sql.setFocus()

    def _sql_typed(self) -> None:
        if self.own_sql.isChecked():
            self._check.start()

    # ---------- copy and preview ----------

    def _copy_sql(self) -> None:
        rows, asset = self.rows(), self.asset()
        if rows is None or asset is None:
            return
        name = f"{file_stem(parquet_name(asset))}.gpkg"
        QApplication.clipboard().setText(parquet_layer.script(rows, name))
        self.copy.setText(_("Copied"))
        QTimer.singleShot(COPIED_MS, lambda: self.copy.setText(_("Copy SQL")))

    def _start_preview(self) -> None:
        rows, asset = self.rows(), self.asset()
        if rows is None or asset is None:
            return
        self._stop_preview()
        read = parquet_layer.previewer(rows, parquet_layer.extension_directory())
        self.preview.setEnabled(False)
        self.footer.setText(_("Reading the first rows…"))
        self.footer.show()

        def done(result: object, error: BaseException | None) -> None:
            if self._preview_task is not task:
                return
            self._preview_task = None
            self._show_state()
            if isinstance(result, parquet_query.Preview):
                self._show_preview(result)
            else:
                self.footer.setText(
                    _("Could not read the rows: {error}").format(
                        error=parquet_query.read_error(rows.url, error)
                    )
                )

        name = parquet_name(asset)
        task = run_task(_("Preview {name} with DuckDB").format(name=name), read, done)
        self._preview_task = task

    def _show_preview(self, preview: parquet_query.Preview) -> None:
        self.table.setModel(preview_model(preview, self.table))
        self.table.resizeColumnsToContents()
        self.table.show()
        rows = len(preview.rows)
        self.footer.setText(
            count("First {n} matching row", "First {n} matching rows", rows)
            if rows
            else _("No row matches in the area of interest.")
        )

    def _stop_preview(self) -> None:
        task, self._preview_task = self._preview_task, None
        if task is not None:
            task.cancel()
