"""The plugin object QGIS loads: menu entry, toolbar button, and dock."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsSettings, QgsVectorTileLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.layers import PMTILES_PROPERTY, vector_tile_layer
from portolan_registry_qgis.qgis_io.network import run_task
from portolan_registry_qgis.qgis_io.tileserver import Archive, TileServer, open_archive

MENU = "&Portolan Registry"
# A QGIS setting, so a user can point the plugin at a registry mirror.
REGISTRY_URL_SETTING = "PortolanRegistry/registry_url"
_ICON = Path(__file__).parent / "icon.svg"


class PortolanRegistryPlugin:
    """Adds the Portolan Registry dock to QGIS."""

    def __init__(self, iface: Any):
        self.iface = iface
        self.server = TileServer()
        self.action: QAction | None = None
        self.dock: Any = None

    def initGui(self) -> None:
        """Create the menu entry and toolbar button. QGIS calls this on load."""
        self.action = QAction(QIcon(str(_ICON)), "Portolan Registry", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip("Browse the Portolan registry")
        self.action.toggled.connect(self._toggle)
        self.iface.addPluginToWebMenu(MENU, self.action)
        self.iface.addWebToolBarIcon(self.action)
        QgsProject.instance().layersAdded.connect(self._reconnect)
        # A GeoParquet layer reads a GeoPackage in the scratch folder. The
        # file goes when the last layer that reads it is removed.
        QgsProject.instance().layersRemoved.connect(self._sweep)
        parquet_layer.remove_stale()

    def unload(self) -> None:
        """Remove everything initGui added. QGIS calls this on unload."""
        with contextlib.suppress(TypeError):
            QgsProject.instance().layersAdded.disconnect(self._reconnect)
        with contextlib.suppress(TypeError):
            QgsProject.instance().layersRemoved.disconnect(self._sweep)
        if self.dock is not None:
            self.dock.disconnect_canvas()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginWebMenu(MENU, self.action)
            self.iface.removeWebToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
        self.server.stop()
        parquet_query.close()
        parquet_layer.close_session()

    @staticmethod
    def _sweep(_layer_ids: list[str]) -> None:
        parquet_layer.sweep()

    def _toggle(self, visible: bool) -> None:
        if self.dock is None:
            from portolan_registry_qgis.core.registry import REGISTRY_URL
            from portolan_registry_qgis.gui.dock import RegistryDock

            url = QgsSettings().value(REGISTRY_URL_SETTING, REGISTRY_URL, type=str)
            self.dock = RegistryDock(self.iface, self.server, url or REGISTRY_URL)
            self.dock.visibilityChanged.connect(self._dock_visibility)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.setVisible(visible)

    def _dock_visibility(self, visible: bool) -> None:
        if self.action is not None and self.action.isChecked() != visible:
            self.action.setChecked(visible)

    def _reconnect(self, layers: list[Any]) -> None:
        """Point PMTiles layers from a saved project at this session's tile server.

        The server picks a new port each session, so the URL a project saved
        no longer answers. The layer keeps the archive URL in a custom
        property, which is enough to register it again.
        """
        for layer in layers:
            if not isinstance(layer, QgsVectorTileLayer):
                continue
            url = layer.customProperty(PMTILES_PROPERTY)
            if not url or self.server.serves(layer.source()):
                continue
            self._reopen(layer, str(url))

    def _reopen(self, layer: QgsVectorTileLayer, url: str) -> None:
        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, Archive):
                QgsMessageLog.logMessage(
                    f"Could not reopen {url}: {error}",
                    "Portolan Registry",
                    Qgis.MessageLevel.Warning,
                )
                return
            fresh = vector_tile_layer(self.server, result, layer.name())
            layer.setDataSource(fresh.source(), layer.name(), fresh.providerType())
            layer.triggerRepaint()

        run_task(f"Reopen {url}", lambda _task: open_archive(url), done)
