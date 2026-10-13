"""The plugin object QGIS loads: menu entry, toolbar button, and window."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsPathResolver,
    QgsProject,
    QgsRasterLayer,
    QgsSettings,
    QgsVectorLayer,
    QgsVectorTileLayer,
)
from qgis.PyQt import sip
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.qgis_io import mosaic, parquet_layer
from portolan_registry_qgis.qgis_io.layers import (
    PMTILES_PROPERTY,
    LayerError,
    apply_bounds,
    vector_tile_layer,
)
from portolan_registry_qgis.qgis_io.network import run_task
from portolan_registry_qgis.qgis_io.tileserver import Archive, TileServer, open_archive

MENU = "&Portolan Registry"
# A QGIS setting, so a user can point the plugin at a registry mirror.
REGISTRY_URL_SETTING = "PortolanRegistry/registry_url"
# A QGIS setting, so a user can point the browser links at a browser mirror.
BROWSER_URL_SETTING = "PortolanRegistry/browser_url"
_ICON = Path(__file__).parent / "icon.svg"  # i18n: skip
# The first QGIS whose QgsPathResolver.setPathPreprocessor binding is safe.
PATH_PREPROCESSOR_VERSION = 33600


class PortolanRegistryPlugin:
    """Adds the Portolan Registry window to QGIS."""

    def __init__(self, iface: Any):
        self.iface = iface
        self.server = TileServer()
        self.action: QAction | None = None
        self.window: Any = None
        self._preprocessor: str | None = None
        self._warned_save = False

    def initGui(self) -> None:
        """Create the menu entry and toolbar button. QGIS calls this on load."""
        self.action = QAction(QIcon(str(_ICON)), "Portolan Registry", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip(_("Browse the Portolan registry"))
        self.action.toggled.connect(self._toggle)
        self.iface.addPluginToWebMenu(MENU, self.action)
        self.iface.addWebToolBarIcon(self.action)
        QgsProject.instance().layersAdded.connect(self._reconnect)
        # A GeoParquet layer reads a GeoPackage in the scratch folder. The
        # file goes when the last layer that reads it is removed.
        QgsProject.instance().layersRemoved.connect(self._sweep)
        QgsProject.instance().writeProject.connect(self._warn_copies)
        # QGIS 3.34 crashes when Python registers a path preprocessor. Its
        # binding builds the return value without the GIL. QGIS 3.36 fixed
        # the binding. On 3.34, a copy whose file is gone opens as an
        # unavailable layer, and _reconnect restores it.
        if Qgis.versionInt() >= PATH_PREPROCESSOR_VERSION:
            self._preprocessor = QgsPathResolver.setPathPreprocessor(parquet_layer.restore_path)
        try:
            parquet_layer.remove_stale()
        except OSError as error:
            _log(_("Could not clean the scratch folder: {error}").format(error=error))

    def unload(self) -> None:
        """Remove everything initGui added. QGIS calls this on unload."""
        with contextlib.suppress(TypeError):
            QgsProject.instance().layersAdded.disconnect(self._reconnect)
        with contextlib.suppress(TypeError):
            QgsProject.instance().layersRemoved.disconnect(self._sweep)
        with contextlib.suppress(TypeError):
            QgsProject.instance().writeProject.disconnect(self._warn_copies)
        if self._preprocessor is not None:
            QgsPathResolver.removePathPreprocessor(self._preprocessor)
            self._preprocessor = None
        if self.window is not None:
            self.window.disconnect_canvas()
            self.window.visibility_changed.disconnect(self._window_visibility)
            self.window.close()
            self.window.deleteLater()
            self.window = None
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

    def _warn_copies(self, _document: object) -> None:
        """Tell the user once that a saved project does not keep GeoParquet edits."""
        layers = QgsProject.instance().mapLayers().values()
        if self._warned_save or not any(
            layer.customProperty(parquet_layer.SOURCE_PROPERTY) for layer in layers
        ):
            return
        self._warned_save = True
        self.iface.messageBar().pushMessage(
            "Portolan Registry",
            _(
                "The project saves each GeoParquet layer as a link to its source file. "
                "When the project opens, QGIS copies the features again, without your edits. "
                "To keep edits, export the layer with Save Features As."
            ),
            Qgis.MessageLevel.Warning,
            0,
        )

    def _toggle(self, visible: bool) -> None:
        if self.window is None:
            from portolan_registry_qgis.core.hrefs import BROWSER_URL
            from portolan_registry_qgis.core.registry import REGISTRY_URL
            from portolan_registry_qgis.gui.window import RegistryWindow

            settings = QgsSettings()
            url = settings.value(REGISTRY_URL_SETTING, REGISTRY_URL, type=str)
            browser = settings.value(BROWSER_URL_SETTING, BROWSER_URL, type=str)
            self.window = RegistryWindow(
                self.iface, self.server, url or REGISTRY_URL, browser or BROWSER_URL
            )
            self.window.visibility_changed.connect(self._window_visibility)
        self.window.setVisible(visible)
        if visible:
            self.window.raise_()
            self.window.activateWindow()

    def _window_visibility(self, visible: bool) -> None:
        if self.action is not None and self.action.isChecked() != visible:
            self.action.setChecked(visible)

    def _reconnect(self, layers: list[Any]) -> None:
        """Reconnect the PMTiles, GeoParquet, and mosaic layers of a saved project.

        The tile server picks a new port each session, so the URL a project
        saved no longer answers. The layer keeps the archive URL in a custom
        property, which is enough to register it again. A GeoParquet layer
        keeps its URL, box, and area, which is enough to copy it again. A
        mosaic keeps the URLs of its COG files, which is enough to write its
        VRT again.
        """
        for layer in layers:
            if parquet_layer.needs_restore(layer):
                self._restore(layer)
            if mosaic.needs_rebuild(layer):
                self._rebuild(layer)
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
                    _("Could not reopen {url}: {error}").format(url=url, error=error),
                    "Portolan Registry",
                    Qgis.MessageLevel.Warning,
                )
                return
            fresh = vector_tile_layer(self.server, result, layer.name())
            layer.setDataSource(fresh.source(), layer.name(), fresh.providerType())
            apply_bounds(layer, result)
            layer.triggerRepaint()

        run_task(_("Reopen {url}").format(url=url), lambda _task: open_archive(url), done)

    def _restore(self, layer: QgsVectorLayer) -> None:
        url, bbox, area = parquet_layer.saved_copy(layer)
        ok, version = parquet_query.duckdb_status()
        if not ok:
            if version:
                _log(
                    # Translators: {version} is the installed DuckDB version, which is too old.
                    _("Could not copy {url} again: DuckDB {version}.").format(
                        url=url, version=version
                    )
                )
            else:
                _log(_("Could not copy {url} again: DuckDB is not installed.").format(url=url))
            return
        extension_dir = parquet_layer.extension_directory()

        def copy(task: Any) -> parquet_layer.Prepared:
            return parquet_layer.restore(url, bbox, extension_dir, task.isCanceled, area)

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_layer.Prepared):
                _log(_("Could not copy {url} again: {error}").format(url=url, error=error))
                return
            if sip.isdeleted(layer):
                parquet_layer.discard_path(result.path)
                return
            try:
                parquet_layer.reopen(layer, result)
            except OSError as failure:
                _log(str(failure))

        run_task(_("Copy {url} again with DuckDB").format(url=url), copy, done)

    def _rebuild(self, layer: QgsRasterLayer) -> None:
        """Write a saved mosaic's VRT again, then point the layer at it.

        GDAL opens each COG to write the VRT, so the write runs in a task.
        """
        hrefs = mosaic.sources(layer)
        clip = mosaic.clip_of(layer)
        name = layer.name()

        def write(task: Any) -> Path:
            return mosaic.write_vrt(hrefs, task.isCanceled, clip)

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, Path):
                _log(
                    _("Could not build the mosaic {name} again: {error}").format(
                        name=name, error=error
                    )
                )
                return
            if sip.isdeleted(layer):
                parquet_layer.discard_path(result)
                return
            try:
                mosaic.reopen(layer, result)
            except LayerError as failure:
                _log(str(failure))

        run_task(_("Build the mosaic {name} again").format(name=name), write, done)


def _log(text: str) -> None:
    QgsMessageLog.logMessage(text, "Portolan Registry", Qgis.MessageLevel.Warning)
