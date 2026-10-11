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
from qgis.gui import QgsCollapsibleGroupBox, QgsMapCanvas, QgsMessageBar
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import QComboBox, QMainWindow, QMessageBox

import portolan_registry_qgis
from portolan_registry_qgis import plugin as plugin_module
from portolan_registry_qgis.gui import pictures
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


_SECTIONS_GROUP = f"QgsCollapsibleGroupBox/{window_module.SETTINGS_GROUP}"


def _forget_layout():
    settings = QgsSettings()
    for key in (window_module.GEOMETRY_SETTING, window_module.SPLITTER_SETTING, _SECTIONS_GROUP):
        settings.remove(key)


@pytest.fixture(autouse=True)
def fresh_layout():
    # The window keeps its size and open sections in the QGIS settings. Each
    # test starts from the defaults and leaves none of its own behind.
    _forget_layout()
    yield
    _forget_layout()


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
    assert window.shown.text() == "1 publisher"
    assert window.search.placeholderText() == "Search publishers by name or license"
    window.search.setText("nothing like this")
    assert rows(window.catalogs) == ["No publisher matches these filters."]
    assert window.shown.text() == "0 of 1 publisher"
    window.search.setText("test")
    assert rows(window.catalogs) == ["Test catalog"]
    # Far from the catalog's extent, the extent filter hides it.
    iface.canvas.setExtent(QgsRectangle(-80, 30, -70, 40))
    window.in_extent.setChecked(True)
    assert rows(window.catalogs) == ["No publisher matches these filters."]
    iface.canvas.setExtent(QgsRectangle(10, 43, 14, 46))
    assert rows(window.catalogs) == ["Test catalog"]
    window.disconnect_canvas()


def test_no_status_filter(iface, server, catalog):
    # Users do not filter by validation status. A catalog that is not valid
    # still shows its status in its row.
    window = open_window(iface, server, catalog)
    assert not hasattr(window, "status")
    assert window.findChildren(QComboBox) == []
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
    # The facts name the publisher and the total size, in plain words. They
    # show no STAC type or ID, and no extent coordinates.
    facts = window.facts.text()
    assert ">Publisher<" in facts and "Test catalog" in facts
    total = sum(asset.size for asset in window._document.assets)
    assert ">Size<" in facts and window_module.human_size(total) in facts
    for hidden in (">Type<", ">ID<", ">Extent<", "11.0000"):
        assert hidden not in facts
    assert window.files_box.title() == f"All files ({len(names)})"
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
    # Before a choice, the details pane shows a hint and no sections.
    assert not window.empty.isHidden()
    assert window.about_box.isHidden()
    open_catalog(window)
    assert window.pages.currentIndex() == 1
    assert window.empty.isHidden()
    assert not window.about_box.isHidden()
    assert window.back.text() == "All publishers"
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
    # The catalog lists no sized files, so the registry's total stands in.
    assert "123.5 KB" in window.facts.text()
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
    assert rows(window.catalogs)[2] == "Show 2 more of 3 publishers"
    window.catalogs.itemClicked.emit(window.catalogs.item(2))
    assert rows(window.catalogs)[2:] == [
        "Test catalog",
        "Test catalog",
        "Show 1 more of 1 publisher",
    ]
    assert window.shown.text() == "5 publishers"
    window.disconnect_canvas()


def test_window_layout_defaults_and_header(iface, server, catalog):
    window = open_window(iface, server, catalog)
    assert window.size() == window_module.DEFAULT_SIZE
    assert window.splitter.widget(0) is window.pages
    assert window.splitter.widget(1).widget().isAncestorOf(window.about_box)
    # The action bar spans both panes, below the splitter.
    assert not window.splitter.isAncestorOf(window.add)
    assert window.tagline.text() == "Open data registry"
    assert not window.logo.pixmap().isNull()
    # The browser link waits until the window can build its URL.
    assert window.open_browser.text() == "Open in browser"
    assert window.open_browser.isHidden()
    assert not window.refresh.isHidden()
    window.disconnect_canvas()


def test_window_restores_its_layout(iface, server, catalog):
    window = open_window(iface, server, catalog)
    window.show()
    window.resize(820, 560)
    window.splitter.setSizes([300, 520])
    QgsApplication.processEvents()
    sizes = window.splitter.sizes()
    window.close()
    assert not QgsSettings().value(window_module.GEOMETRY_SETTING).isEmpty()
    window.disconnect_canvas()

    again = open_window(iface, server, catalog)
    again.show()
    QgsApplication.processEvents()
    assert again.size() == QSize(820, 560)
    assert again.splitter.sizes() == sizes
    again.close()
    again.disconnect_canvas()


def test_sections_collapse_and_remember(iface, server, catalog):
    window = open_window(iface, server, catalog)
    boxes = {
        "About": window.about_box,
        "Add to map": window.add_box,
        "All files": window.files_box,
        "Details": window.details_box,
    }
    for title, box in boxes.items():
        assert isinstance(box, QgsCollapsibleGroupBox)
        assert box.title() == title
        assert box.settingGroup() == window_module.SETTINGS_GROUP
        assert not box.isCollapsed()
    open_collection(window)
    window.show()
    QgsApplication.processEvents()
    window.details_box.setCollapsed(True)
    # A collapse hides the content, but the window's own choices stay.
    assert not window.facts.isVisible()
    assert not window.facts.isHidden()
    window.close()
    window.disconnect_canvas()

    again = open_window(iface, server, catalog)
    again.show()
    QgsApplication.processEvents()
    assert again.details_box.isCollapsed()
    assert not again.about_box.isCollapsed()
    again.details_box.setCollapsed(False)
    again.close()
    again.disconnect_canvas()


def test_hero_shows_thumbnail_or_extent(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_catalog(window)
    # The catalog has no thumbnail, so the hero draws its extent.
    wait_for(lambda: window._document is not None and window._document.kind == "catalog")
    window.hero.grab()
    assert window.hero.painted_extent
    window.tree.setCurrentItem(window.tree.topLevelItem(0))
    wait_for(lambda: window.hero.has_picture)
    # With a thumbnail, the hero draws the picture alone, with no inset map.
    window.hero.grab()
    assert not window.hero.painted_extent
    window.disconnect_canvas()


def test_logomark_follows_the_theme(iface, server, catalog):
    for path in (pictures.LOGOMARK_LIGHT, pictures.LOGOMARK_DARK):
        assert path.is_file()
        assert path.parent == Path(portolan_registry_qgis.__file__).parent / "images"
    light, dark = QPalette(), QPalette()
    light.setColor(QPalette.ColorRole.Base, QColor("#fcfcfa"))
    dark.setColor(QPalette.ColorRole.Base, QColor("#16170f"))
    assert pictures.logomark(light) == pictures.LOGOMARK_LIGHT
    assert pictures.logomark(dark) == pictures.LOGOMARK_DARK

    window = open_window(iface, server, catalog)
    window.setPalette(light)
    assert window.logomark_file == pictures.LOGOMARK_LIGHT
    window.setPalette(dark)
    assert window.logomark_file == pictures.LOGOMARK_DARK
    assert not window.logo.pixmap().isNull()
    window.disconnect_canvas()
