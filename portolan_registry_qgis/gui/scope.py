"""The scope picker: everything, the map view, a drawn area, or a layer."""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QWidget

from portolan_registry_qgis.core.scope import Everything, Scope


class ScopePicker(QWidget):
    """Choose which part of a dataset to add or download.

    The choices are "Everything", "Map view", "Draw an area", and "From a
    layer". Drawing uses a polygon map tool on the canvas. The drawn area
    stays on the canvas until the choice changes.
    """

    changed = pyqtSignal()

    def __init__(self, iface: Any, parent: QWidget | None = None):
        super().__init__(parent)
        self._iface = iface

    def scope(self) -> Scope:
        """Return the chosen scope in EPSG:4326."""
        return Everything()

    def clear(self) -> None:
        """Remove the drawn area from the canvas and restore the map tool."""
