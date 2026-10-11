from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsVectorTileLayer,
)
from qgis.gui import QgsMapCanvas, QgsMessageBar
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox

import portolan_registry_qgis
from portolan_registry_qgis import plugin as plugin_module
from portolan_registry_qgis.gui import window as window_module
from portolan_registry_qgis.gui.widgets import BADGE_ROLE
from portolan_registry_qgis.gui.window import RegistryWindow
from portolan_registry_qgis.plugin import REGISTRY_URL_SETTING
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.layers import PMTILES_PROPERTY
from portolan_registry_qgis.qgis_io.tileserver import TileServer

from .conftest import wait_for


class FakeIface:
    """The parts of QgisInterface the plugin calls."""

    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.canvas.resize(400, 400)
        self.bar = QgsMessageBar()
        self.menu = []
        self.toolbar = []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def addPluginToWebMenu(self, _menu, action):
        self.menu.append(action)

    def removePluginWebMenu(self, _menu, action):
        self.menu.remove(action)

    def addWebToolBarIcon(self, action):
        self.toolbar.append(action)

    def removeWebToolBarIcon(self, action):
        self.toolbar.remove(action)


@pytest.fixture
def iface():
    fake = FakeIface()
    yield fake
    QgsProject.instance().clear()
    for widget in (fake.canvas, fake.bar, fake.window):
        widget.deleteLater()
    QgsApplication.processEvents()


@pytest.fixture
def server():
    tiles = TileServer()
    yield tiles
    tiles.stop()


def rows(view):
    if hasattr(view, "topLevelItem"):
        return [view.topLevelItem(i).text(0) for i in range(view.topLevelItemCount())]
    return [view.item(i).text() for i in range(view.count())]


def shown(window):
    return " ".join((window.title.text(), window.description.text(), window.facts.text()))


def open_window(iface, server, catalog):
    window = RegistryWindow(iface, server, f"{catalog['base']}/registry.json")
    wait_for(lambda: rows(window.catalogs) == ["Test catalog"])
    return window


def open_catalog(window):
    window.catalogs.itemClicked.emit(window.catalogs.item(0))
    wait_for(lambda: rows(window.tree) == ["Test points", "Missing"])


def open_collection(window):
    open_catalog(window)
    window.tree.setCurrentItem(window.tree.topLevelItem(0))
    wait_for(lambda: window.assets.topLevelItemCount() > 0)


def test_registry_lists_and_filters(iface, server, catalog):
    window = open_window(iface, server, catalog)
    assert window.status.itemText(1) == "Valid"
    window.search.setText("nothing like this")
    assert rows(window.catalogs) == ["No catalog matches these filters."]
    window.search.setText("test")
    assert rows(window.catalogs) == ["Test catalog"]
    # Far from the catalog's extent, the extent filter hides it.
    iface.canvas.setExtent(QgsRectangle(-80, 30, -70, 40))
    window.in_extent.setChecked(True)
    assert rows(window.catalogs) == ["No catalog matches these filters."]
    iface.canvas.setExtent(QgsRectangle(10, 43, 14, 46))
    assert rows(window.catalogs) == ["Test catalog"]
    window.disconnect_canvas()


def test_unreachable_registry_says_so(iface, server, catalog):
    window = RegistryWindow(iface, server, f"{catalog['base']}/gone.json")
    wait_for(lambda: rows(window.catalogs)[0].startswith("Could not read the Portolan registry"))
    window.disconnect_canvas()


def test_tree_details_and_assets(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    names = rows(window.assets)
    assert names[0] == "Point tiles"
    assert "geojson" in names
    # Assets QGIS can open come first, and each row carries its format badge.
    assert names[-3:] == ["style-red", "style-blue", "thumbnail"]
    badges = {
        window.assets.topLevelItem(i).text(0): window.assets.topLevelItem(i).data(0, BADGE_ROLE)[0]
        for i in range(len(names))
    }
    assert badges["data"] == "GeoParquet"
    assert badges["relief"] == "COG"
    assert badges["thumbnail"] == "PNG"
    assert "Test points" in shown(window)
    assert "CC-BY-4.0" in shown(window)
    assert "2 styles" in window.tile_styles.text()
    assert not window.tile_styles.isHidden()
    assert not window.parquet_extent.isHidden()
    assert window.zoom.isEnabled()
    assert window.download_all.isEnabled()
    # The first PMTiles link is selected, so Add to map works at once.
    assert [row.text(0) for row in window.assets.selectedItems()] == ["Point tiles"]
    assert window.add.isEnabled()
    # The missing collection reports its failure in the details pane.
    window.tree.setCurrentItem(window.tree.topLevelItem(1))
    wait_for(lambda: "Could not read" in window.description.text())
    window.disconnect_canvas()


def test_pictures_and_navigation(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_catalog(window)
    assert window.pages.currentIndex() == 1
    assert window.header.title.text() == "Test catalog"
    assert "1 collection" in window.header.summary.text()
    # The registry's logo shows in the header, and the collection's
    # currentColor icon replaces its kind icon in the tree.
    wait_for(lambda: not window.header.logo.isHidden())
    icon_url = f"{catalog['base']}/leaf.svg"
    wait_for(lambda: window.images.get(icon_url) is not None)
    assert window.images.get(icon_url).monochrome
    window.tree.setCurrentItem(window.tree.topLevelItem(0))
    wait_for(lambda: window.hero.has_picture)
    # The header brings back the catalog's own details.
    window.header.clicked.emit()
    wait_for(lambda: window._document is not None and window._document.kind == "catalog")
    assert "no files of its own" in window.no_assets.text()
    window.back.click()
    assert window.pages.currentIndex() == 0
    # Opening the same catalog again keeps its tree.
    window.catalogs.itemClicked.emit(window.catalogs.item(0))
    assert rows(window.tree) == ["Test points", "Missing"]
    window.disconnect_canvas()


def asset_row(window, name):
    return next(
        window.assets.topLevelItem(i)
        for i in range(window.assets.topLevelItemCount())
        if window.assets.topLevelItem(i).text(0) == name
    )


def test_add_styled_tiles_and_assets(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    # Zoom out first, so the map extent holds every point.
    iface.canvas.setExtent(QgsRectangle(0, 30, 30, 60))
    window.parquet_extent.setChecked(True)
    for name in ("Point tiles", "geojson", "data"):
        asset_row(window, name).setSelected(True)
    assert window.add.isEnabled()
    window.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 3, timeout=60)
    added = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values()}
    tiles = added["Point tiles"]
    assert isinstance(tiles, QgsVectorTileLayer)
    assert tiles.styleManager().currentStyle() == "style-red"
    assert tiles.customProperty(PMTILES_PROPERTY).endswith("points.pmtiles")
    assert added["geojson"].isValid()
    assert added["points"].featureCount() == 200
    window.zoom.click()
    extent = iface.canvas.extent()
    assert extent.contains(QgsRectangle(11.5, 44.2, 12.5, 44.8))
    window.disconnect_canvas()


def test_parquet_in_a_small_extent(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    iface.canvas.setExtent(QgsRectangle(11.095, 44.0, 11.305, 45.0))
    window.assets.clearSelection()
    asset_row(window, "data").setSelected(True)
    window.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    (layer,) = QgsProject.instance().mapLayers().values()
    assert 0 < layer.featureCount() < 200
    window.disconnect_canvas()


def test_large_parquet_asks_first(iface, server, catalog, monkeypatch):
    asked = []
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]

    def question(_parent, _title, text):
        asked.append(text)
        return answers[len(asked) - 1]

    monkeypatch.setattr(window_module, "CONFIRM_FEATURES", 100)
    monkeypatch.setattr(window_module.QMessageBox, "question", question)
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.parquet_extent.setChecked(False)
    window.assets.clearSelection()
    asset_row(window, "data").setSelected(True)
    window.add.click()
    wait_for(lambda: len(asked) == 1, timeout=60)
    assert "about 200 features in the file" in asked[0]
    assert QgsProject.instance().mapLayers() == {}
    window.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    (layer,) = QgsProject.instance().mapLayers().values()
    assert layer.featureCount() == 200
    window.disconnect_canvas()


def test_tooltips_say_which_asset_to_use(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    assert window_module.VIEW_OR_ANALYZE in window.add.toolTip()
    assert window_module.VIEW_OR_ANALYZE in asset_row(window, "data").toolTip(0)
    assert window_module.VIEW_OR_ANALYZE in asset_row(window, "Point tiles").toolTip(0)
    assert window_module.VIEW_OR_ANALYZE not in asset_row(window, "geojson").toolTip(0)
    window.disconnect_canvas()


def test_no_pmtiles_advice_without_tiles(iface, server, catalog, monkeypatch):
    monkeypatch.setattr(window_module.layer_io, "archive_urls", lambda _document: [])
    window = open_window(iface, server, catalog)
    open_collection(window)
    assert window_module.VIEW_OR_ANALYZE not in window.add.toolTip()
    assert window_module.VIEW_OR_ANALYZE not in asset_row(window, "data").toolTip(0)
    window.disconnect_canvas()


def test_missing_duckdb_shows_install_help(iface, server, catalog, monkeypatch):
    shown = []
    monkeypatch.setattr(window_module.parquet_query, "duckdb_status", lambda: (False, None))
    monkeypatch.setenv("FLATPAK_ID", "org.qgis.qgis")
    monkeypatch.setattr(window_module.QMessageBox, "exec", lambda box: shown.append(box.text()))
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.assets.clearSelection()
    asset_row(window, "data").setSelected(True)
    window.add.click()
    assert len(shown) == 1
    assert "flatpak run --command=python3 org.qgis.qgis" in shown[0]
    assert "not installed" in shown[0]
    assert QgsProject.instance().mapLayers() == {}
    window.disconnect_canvas()


def test_download_all(iface, server, catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(window_module.QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    asked = []

    def answer(*args):
        asked.append(args[2])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(window_module.QMessageBox, "question", answer)
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.header.clicked.emit()
    wait_for(lambda: window._document is not None and window._document.kind == "catalog")
    window.download_all.trigger()
    wait_for(lambda: window._job is None and asked and not window.cancel.isVisible(), timeout=60)
    assert "could not be read" in asked[0]
    assert (tmp_path / "catalog.json").is_file()
    assert (tmp_path / "points" / "collection.json").is_file()
    assert (tmp_path / "points.pmtiles").is_file()
    assert (tmp_path / "points" / "styles" / "red.json").is_file()
    window.disconnect_canvas()


def test_cancelled_listing_asks_nothing(iface, server, catalog, tmp_path, monkeypatch):
    # The walk returns a partial plan when it is cancelled. The window must not
    # offer that partial plan for download.
    monkeypatch.setattr(window_module.QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    asked = []
    monkeypatch.setattr(window_module.QMessageBox, "question", lambda *a: asked.append(a))
    real = window_module.fetch_json

    reading = threading.Event()

    def slow(url):
        reading.set()
        time.sleep(0.5)
        return real(url)

    window = open_window(iface, server, catalog)
    open_collection(window)
    window.header.clicked.emit()
    wait_for(lambda: window._document is not None and window._document.kind == "catalog")
    monkeypatch.setattr(window_module, "fetch_json", slow)
    manager = QgsApplication.taskManager()
    window.download_all.trigger()
    # Cancel during the first read, so the walk returns a partial plan.
    wait_for(reading.is_set)
    manager.cancelAll()
    wait_for(lambda: window.progress.isHidden() and manager.countActiveTasks() == 0)
    for _ in range(20):
        QgsApplication.processEvents()
    assert asked == []
    assert window._job is None
    window.disconnect_canvas()


def test_plugin_lifecycle(iface, catalog):
    settings = QgsSettings()
    settings.setValue(REGISTRY_URL_SETTING, f"{catalog['base']}/registry.json")
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    assert len(iface.menu) == 1
    assert Path(portolan_registry_qgis.__file__).with_name("icon.svg").is_file()
    plugin.action.setChecked(True)
    window = plugin.window
    assert window.isVisible()
    assert window.parent() is iface.window
    assert not window.isModal()
    wait_for(lambda: rows(window.catalogs) == ["Test catalog"])
    # Closing the window clears the toolbar button.
    window.close()
    assert not plugin.action.isChecked()
    plugin.action.setChecked(True)
    assert window.isVisible()
    settings.remove(REGISTRY_URL_SETTING)
    plugin.unload()
    assert iface.menu == []
    assert iface.toolbar == []
    assert plugin.window is None


def test_plugin_reconnects_saved_tiles(iface, catalog):
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    # A layer as a saved project restores it: the old port no longer answers.
    stale = QgsVectorTileLayer("type=xyz&url=http://127.0.0.1:9/old/{z}/{x}/{y}.pbf", "saved")
    stale.setCustomProperty(PMTILES_PROPERTY, f"{catalog['base']}/points.pmtiles")
    stale.styleManager().addStyleFromLayer("style-red")
    QgsProject.instance().addMapLayer(stale)
    wait_for(lambda: plugin.server.serves(stale.source()))
    assert stale.isValid()
    # The catalog styles a project saved survive the reconnect.
    assert "style-red" in stale.styleManager().styles()
    plugin.unload()


@pytest.mark.parametrize("preprocessor", [True, False], ids=["preprocessor", "qgis-3.34"])
def test_plugin_restores_saved_parquet(iface, catalog, tmp_path, monkeypatch, preprocessor):
    if not preprocessor:
        # QGIS 3.34 crashes when Python registers a path preprocessor, so the
        # plugin skips it there. Restore must work without it.
        monkeypatch.setattr(plugin_module, "PATH_PREPROCESSOR_VERSION", 10**9)
    elif Qgis.versionInt() < plugin_module.PATH_PREPROCESSOR_VERSION:
        pytest.skip("This QGIS cannot register a path preprocessor")
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    assert (plugin._preprocessor is not None) is preprocessor
    project = QgsProject.instance()
    context = project.transformContext()
    extent = QgsRectangle(11.095, 44.0, 11.305, 45.0)
    planned = parquet_layer.plan(
        f"{catalog['base']}/points.parquet",
        context,
        extent,
        QgsCoordinateReferenceSystem("EPSG:4326"),
    )
    prepared = parquet_layer.prepare(planned)
    layer, _ = parquet_layer.build(prepared, "points")
    project.addMapLayer(layer)
    saved = tmp_path / "saved.qgs"
    assert project.write(str(saved))
    # The save tells the user that edits to the copy are not kept.
    assert any("without your edits" in item.text() for item in iface.bar.items())
    project.clear()
    # Closing the project removes the layer, and the sweep deletes its copy.
    assert not prepared.path.exists()
    assert project.read(str(saved))
    (restored,) = project.mapLayers().values()
    # With the preprocessor, the placeholder opens, so QGIS reports no
    # unavailable layer. Without it, the layer opens unavailable.
    assert restored.isValid() is preprocessor
    wait_for(lambda: restored.isValid() and restored.featureCount() == 21, timeout=60)
    assert not parquet_layer.needs_restore(restored)
    assert sorted(f["id"] for f in restored.getFeatures()) == list(range(10, 31))
    plugin.unload()


def test_lists_show_one_page_at_a_time(iface, server, catalog, monkeypatch):
    monkeypatch.setattr(window_module, "NODE_PAGE", 1)
    monkeypatch.setattr(window_module, "CATALOG_PAGE", 2)
    window = open_window(iface, server, catalog)
    window.catalogs.itemClicked.emit(window.catalogs.item(0))
    wait_for(lambda: rows(window.tree) == ["Test points", "Show 1 more of 1"])
    window.tree.itemClicked.emit(window.tree.topLevelItem(1), 0)
    assert rows(window.tree) == ["Test points", "Missing"]
    # Five copies of the one registered catalog fill two pages and a half.
    window._entries = window._entries * 5
    window._show_catalogs()
    assert rows(window.catalogs)[2] == "Show 2 more of 3"
    window.catalogs.itemClicked.emit(window.catalogs.item(2))
    assert rows(window.catalogs)[2:] == ["Test catalog", "Test catalog", "Show 1 more of 1"]
    assert window.shown.text() == "5 catalogs"
    window.disconnect_canvas()
