"""The table model of the first rows that a GeoParquet load keeps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel

if TYPE_CHECKING:
    from qgis.PyQt.QtCore import QObject

    from portolan_registry_qgis.core.parquet_query import Preview


def cell_text(value: object) -> str:
    """Return the text of one cell. NULL shows as an empty cell."""
    return "" if value is None else str(value)


def preview_model(preview: Preview, parent: QObject | None = None) -> QStandardItemModel:
    """Return a read-only table model of the rows of ``preview``."""
    model = QStandardItemModel(len(preview.rows), len(preview.columns), parent)
    model.setHorizontalHeaderLabels(list(preview.columns))
    for row, values in enumerate(preview.rows):
        for column, value in enumerate(values):
            item = QStandardItem(cell_text(value))
            item.setEditable(False)
            model.setItem(row, column, item)
    return model
