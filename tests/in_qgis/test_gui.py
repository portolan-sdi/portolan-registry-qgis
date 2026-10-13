from __future__ import annotations

import threading
import time
from html import escape as html_escape
from pathlib import Path

import pytest
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsVectorLayer,
    QgsVectorTileLayer,
)
from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor, QDesktopServices, QPalette
from qgis.PyQt.QtWidgets import QComboBox, QFileDialog, QMessageBox, QPushButton

import portolan_registry_qgis
from portolan_registry_qgis import plugin as plugin_module
from portolan_registry_qgis.core import facts as facts_module
from portolan_registry_qgis.core import loadable, parquet_query
from portolan_registry_qgis.core.format import human_size
from portolan_registry_qgis.core.stac import read_document
from portolan_registry_qgis.gui import downloads as downloads_module
from portolan_registry_qgis.gui import loading as loading_module
from portolan_registry_qgis.gui import pictures
from portolan_registry_qgis.gui import registry_list as registry_list_module
from portolan_registry_qgis.gui import widgets as widgets_module
from portolan_registry_qgis.gui import window as window_module
from portolan_registry_qgis.gui.scope import EVERYTHING, FROM_LAYER, MAP_VIEW
from portolan_registry_qgis.gui.widgets import BADGE_ROLE, ON_MAP_ROLE, ROLE
from portolan_registry_qgis.gui.window import RegistryWindow
from portolan_registry_qgis.plugin import BROWSER_URL_SETTING, REGISTRY_URL_SETTING
from portolan_registry_qgis.qgis_io import mosaic, network, parquet_layer
from portolan_registry_qgis.qgis_io.downloader import DownloadJob
from portolan_registry_qgis.qgis_io.layers import PMTILES_PROPERTY
from portolan_registry_qgis.qgis_io.tileserver import TileServer

from .conftest import wait_for

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
def asked(tmp_path, monkeypatch):
    """Download into ``tmp_path`` and answer Yes to each question.

    The list collects the text of each question the window asks.
    """
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    texts = []

    def answer(*args):
        texts.append(args[2])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", answer)
    return texts


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
    return " ".join(
        (
            window.details.title.text(),
            window.details.description.text(),
            window.details.facts.text(),
        )
    )


def open_window(iface, server, catalog):
    window = RegistryWindow(iface, server, f"{catalog['base']}/registry.json")
    wait_for(lambda: rows(window.registry_list.catalogs) == ["Test catalog"])
    return window


def open_catalog(window):
    window.registry_list.catalogs.itemClicked.emit(window.registry_list.catalogs.item(0))
    wait_for(lambda: rows(window.catalog_tree.tree) == ["Test points", "Missing"])


def open_collection(window):
    open_catalog(window)
    window.catalog_tree.tree.setCurrentItem(window.catalog_tree.tree.topLevelItem(0))
    wait_for(lambda: window.details.assets.topLevelItemCount() > 0)


def test_registry_lists_and_filters(iface, server, catalog):
    window = open_window(iface, server, catalog)
    assert window.registry_list.shown.text() == "1 publisher"
    assert window.registry_list.search.placeholderText() == "Search publishers by name or license"
    window.registry_list.search.setText("nothing like this")
    assert rows(window.registry_list.catalogs) == ["No publisher matches these filters."]
    assert window.registry_list.shown.text() == "0 of 1 publisher"
    window.registry_list.search.setText("test")
    assert rows(window.registry_list.catalogs) == ["Test catalog"]
    # Far from the catalog's extent, the extent filter hides it.
    iface.canvas.setExtent(QgsRectangle(-80, 30, -70, 40))
    window.registry_list.in_extent.setChecked(True)
    assert rows(window.registry_list.catalogs) == ["No publisher matches these filters."]
    iface.canvas.setExtent(QgsRectangle(10, 43, 14, 46))
    assert rows(window.registry_list.catalogs) == ["Test catalog"]
    window.disconnect_canvas()


def test_no_status_filter(iface, server, catalog):
    # Users do not filter by validation status. A catalog that is not valid
    # still shows its status in its row.
    window = open_window(iface, server, catalog)
    assert not hasattr(window, "status")
    assert window.pages.widget(0).findChildren(QComboBox) == []
    window.disconnect_canvas()


def test_unreachable_registry_says_so(iface, server, catalog):
    window = RegistryWindow(iface, server, f"{catalog['base']}/gone.json")
    wait_for(
        lambda: rows(window.registry_list.catalogs)[0].startswith(
            "Could not read the Portolan registry"
        )
    )
    window.disconnect_canvas()


@pytest.fixture
def collection_window(iface, server, catalog):
    """A window that shows the points collection."""
    window = open_window(iface, server, catalog)
    open_collection(window)
    yield window
    window.disconnect_canvas()


def test_assets_show_only_files_qgis_opens(collection_window):
    window = collection_window
    names = rows(window.details.assets)
    # Only the files QGIS can open show. The archive that is both a link and
    # the "visual" asset shows once. Styles and the thumbnail do not show.
    assert names == ["Point tiles", "geojson", "relief", "data"]
    badges = {
        name: window.details.assets.topLevelItem(i).data(0, BADGE_ROLE)[0]
        for i, name in enumerate(names)
    }
    assert badges["Point tiles"] == "PMTiles"
    assert badges["data"] == "GeoParquet"
    assert badges["relief"] == "COG"
    assert window.details.files_box.title() == f"All files ({len(names)})"
    assert "2 styles" in window.details.tile_styles.text()
    assert not window.details.tile_styles.isHidden()


def test_details_show_plain_facts(collection_window):
    window = collection_window
    assert "Test points" in shown(window)
    assert "CC-BY-4.0" in shown(window)
    # The facts name the publisher and the total size, in plain words. They
    # show no STAC type or ID, and no extent coordinates.
    facts = window.details.facts.text()
    assert ">Publisher<" in facts
    assert "Test catalog" in facts
    total = sum(asset.size for asset in window.document.assets)
    assert ">Size<" in facts
    assert human_size(total) in facts
    for hidden in (">Type<", ">ID<", ">Extent<", "11.0000"):
        assert hidden not in facts


def test_details_buttons_start_in_their_sections(collection_window):
    details = collection_window.details
    assert not details.scope_row.isHidden()
    assert details.zoom.isEnabled()
    assert details.download_all.isEnabled()
    # All files acts only on rows the user selects, so nothing starts
    # selected, and its buttons wait.
    assert details.assets.selectedItems() == []
    assert not details.add.isEnabled()
    assert not details.download_selected.isEnabled()
    assert details.add.text() == "Add selected"
    # Each section holds its own buttons. Add to map holds the area first.
    assert details.files_box.isAncestorOf(details.add)
    assert details.files_box.isAncestorOf(details.download_files)
    assert details.add_box.isAncestorOf(details.download_area)
    # Zoom to and the browser link sit next to the dataset's name.
    assert details.title_row.isAncestorOf(details.zoom)
    assert details.title_row.isAncestorOf(details.open_browser)
    add_layout = details.scope_row.parentWidget().layout()
    assert add_layout.indexOf(details.scope_row) == 0


def test_a_missing_collection_says_so_in_the_details(collection_window):
    window = collection_window
    window.catalog_tree.tree.setCurrentItem(window.catalog_tree.tree.topLevelItem(1))
    wait_for(lambda: "Could not read" in window.details.description.text())


def test_pictures_and_navigation(iface, server, catalog):
    window = open_window(iface, server, catalog)
    # Before a choice, the details pane shows a hint and no sections.
    assert not window.details.empty.isHidden()
    assert window.details.about_box.isHidden()
    open_catalog(window)
    assert window.pages.currentIndex() == 1
    assert window.details.empty.isHidden()
    assert not window.details.about_box.isHidden()
    assert window.catalog_tree.back.text() == "All publishers"
    assert window.catalog_tree.header.title.text() == "Test catalog"
    assert "1 collection" in window.catalog_tree.header.summary.text()
    # The registry's logo shows in the header, and the collection's
    # currentColor icon replaces its kind icon in the tree.
    wait_for(lambda: not window.catalog_tree.header.logo.isHidden())
    icon_url = f"{catalog['base']}/leaf.svg"
    wait_for(lambda: window.images.get(icon_url) is not None)
    assert window.images.get(icon_url).monochrome
    window.catalog_tree.tree.setCurrentItem(window.catalog_tree.tree.topLevelItem(0))
    wait_for(lambda: window.details.hero.has_picture)
    # The header brings back the catalog's own details.
    window.catalog_tree.header.clicked.emit()
    wait_for(lambda: window.document is not None and window.document.kind == "catalog")
    assert "no files of its own" in window.details.no_assets.text()
    # The catalog lists no sized files, so the registry's total stands in.
    assert "123.5 KB" in window.details.facts.text()
    window.catalog_tree.back.click()
    assert window.pages.currentIndex() == 0
    # Opening the same catalog again keeps its tree.
    window.registry_list.catalogs.itemClicked.emit(window.registry_list.catalogs.item(0))
    assert rows(window.catalog_tree.tree) == ["Test points", "Missing"]
    window.disconnect_canvas()


def asset_row(window, name):
    return next(
        window.details.assets.topLevelItem(i)
        for i in range(window.details.assets.topLevelItemCount())
        if window.details.assets.topLevelItem(i).text(0) == name
    )


def test_add_styled_tiles_and_assets(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    # Zoom out first, so the map extent holds every point.
    iface.canvas.setExtent(QgsRectangle(0, 30, 30, 60))
    window.details.scope_picker.choose(MAP_VIEW)
    for name in ("Point tiles", "geojson", "data"):
        asset_row(window, name).setSelected(True)
    assert window.details.add.isEnabled()
    window.details.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 3, timeout=60)
    added = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values()}
    tiles = added["Point tiles"]
    assert isinstance(tiles, QgsVectorTileLayer)
    assert tiles.styleManager().currentStyle() == "style-red"
    assert tiles.customProperty(PMTILES_PROPERTY).endswith("points.pmtiles")
    assert added["geojson"].isValid()
    assert added["points"].featureCount() == 200
    window.details.zoom.click()
    extent = iface.canvas.extent()
    assert extent.contains(QgsRectangle(11.5, 44.2, 12.5, 44.8))
    window.disconnect_canvas()


def test_parquet_in_a_small_extent(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    iface.canvas.setExtent(QgsRectangle(11.095, 44.0, 11.305, 45.0))
    window.details.scope_picker.choose(MAP_VIEW)
    # Load full data reads only the area.
    window.details.load_button.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    (layer,) = QgsProject.instance().mapLayers().values()
    assert 0 < layer.featureCount() < 200
    # Add selected under All files adds the whole file, whatever the area.
    asset_row(window, "data").setSelected(True)
    window.details.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 2, timeout=60)
    whole = max(QgsProject.instance().mapLayers().values(), key=lambda layer: layer.featureCount())
    assert whole.featureCount() == 200
    window.disconnect_canvas()


def test_large_parquet_asks_first(iface, server, catalog, monkeypatch):
    asked = []
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]

    def question(_parent, _title, text):
        asked.append(text)
        return answers[len(asked) - 1]

    monkeypatch.setattr(loading_module, "CONFIRM_FEATURES", 100)
    monkeypatch.setattr(QMessageBox, "question", question)
    window = open_window(iface, server, catalog)
    open_collection(window)
    assert window.details.scope_picker.choice() == EVERYTHING
    window.details.assets.clearSelection()
    asset_row(window, "data").setSelected(True)
    window.details.add.click()
    wait_for(lambda: len(asked) == 1, timeout=60)
    assert "about 200 features in the file" in asked[0]
    assert QgsProject.instance().mapLayers() == {}
    window.details.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    (layer,) = QgsProject.instance().mapLayers().values()
    assert layer.featureCount() == 200
    window.disconnect_canvas()


def test_two_buttons_say_which_file_to_use(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    # The advice lives in the two buttons, not in the file rows.
    assert not hasattr(window_module, "VIEW_OR_ANALYZE")
    assert window.details.view_button.text() == "View on map"
    assert window.details.load_button.text() == "Load full data"
    # Each tooltip names the format first.
    assert window.details.view_button.toolTip().startswith("Adds the vector tiles (PMTiles).")
    assert window.details.load_button.toolTip().startswith("Loads the GeoParquet.")
    assert loadable.VIEW_TIP in window.details.view_button.toolTip()
    assert "Point tiles (PMTiles)" in window.details.view_button.toolTip()
    assert loadable.LOAD_TIP in window.details.load_button.toolTip()
    assert "data (GeoParquet)" in window.details.load_button.toolTip()
    for name in ("data", "Point tiles", "geojson"):
        tip = asset_row(window, name).toolTip(0)
        assert loadable.VIEW_TIP not in tip and loadable.LOAD_TIP not in tip
    assert window.details.mosaic_button.isHidden()
    window.disconnect_canvas()


def test_view_on_map_adds_the_tiles_and_load_full_data_the_parquet(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.details.view_button.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    (tiles,) = QgsProject.instance().mapLayers().values()
    assert isinstance(tiles, QgsVectorTileLayer)
    window.details.load_button.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 2, timeout=60)
    added = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values()}
    assert added["points"].featureCount() == 200
    window.disconnect_canvas()


def add_whole(window, name):
    """Add the file in row ``name`` with Add selected."""
    window.details.assets.clearSelection()
    asset_row(window, name).setSelected(True)
    window.details.add.click()


def test_a_layer_outside_the_map_moves_the_map(iface, server, catalog):
    # The partido of Pergamino loaded, but the map showed another place, so
    # the layer looked as if it did not load. QGIS zooms to a new layer only
    # in an empty project.
    window = open_window(iface, server, catalog)
    open_collection(window)
    QgsProject.instance().addMapLayer(QgsVectorLayer("Point?crs=EPSG:4326", "other", "memory"))
    far = QgsRectangle(100, 0, 101, 1)
    iface.canvas.setExtent(far)
    add_whole(window, "geojson")
    added = QgsProject.instance().mapLayersByName("geojson")[0]
    assert iface.canvas.extent().intersects(added.extent())
    assert any("The map moved to show it." in item.text() for item in iface.bar.items())
    # A layer that the map shows already leaves the map where it is.
    view = QgsRectangle(added.extent())
    view.grow(0.5)
    iface.canvas.setExtent(view)
    before = iface.canvas.extent()
    add_whole(window, "geojson")
    assert iface.canvas.extent() == before
    window.disconnect_canvas()


def test_buttons_fall_back_to_other_formats(iface, server, catalog, monkeypatch):
    # Without PMTiles, View on map adds the COG. Without DuckDB, Load full
    # data adds the GeoJSON instead of the GeoParquet.
    monkeypatch.setattr(parquet_query, "duckdb_status", lambda: (False, None))
    window = open_window(iface, server, catalog)
    base = catalog["base"]
    href = f"{base}/points/collection.json"
    raw = network.fetch_json(href)
    raw["links"] = [link for link in raw["links"] if link["rel"] != "pmtiles"]
    raw["assets"].pop("visual")
    window.show_document(read_document(raw, href))
    assert window.details.view_button.toolTip().endswith("relief (COG)")
    assert window.details.load_button.toolTip().endswith("geojson (GeoJSON)")
    assert window.details.tile_styles.isHidden()
    window.details.view_button.click()
    window.details.load_button.click()
    names = sorted(layer.name() for layer in QgsProject.instance().mapLayers().values())
    assert names == ["geojson", "relief"]
    window.disconnect_canvas()


def test_missing_duckdb_shows_install_help(iface, server, catalog, monkeypatch):
    shown = []
    monkeypatch.setattr(parquet_query, "duckdb_status", lambda: (False, None))
    monkeypatch.setenv("FLATPAK_ID", "org.qgis.qgis")
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()))
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.details.assets.clearSelection()
    asset_row(window, "data").setSelected(True)
    window.details.add.click()
    assert len(shown) == 1
    assert "flatpak run --command=python3 org.qgis.qgis" in shown[0]
    assert "not installed" in shown[0]
    assert QgsProject.instance().mapLayers() == {}
    window.disconnect_canvas()


def test_download_all(iface, server, catalog, tmp_path, asked):
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.catalog_tree.header.clicked.emit()
    wait_for(lambda: window.document is not None and window.document.kind == "catalog")
    window.details.download_all.trigger()
    wait_for(
        lambda: not window.is_busy() and asked and not window.tasks.cancel.isVisible(), timeout=60
    )
    assert "could not be read" in asked[0]
    assert (tmp_path / "catalog.json").is_file()
    assert (tmp_path / "points" / "collection.json").is_file()
    assert (tmp_path / "points.pmtiles").is_file()
    assert (tmp_path / "points" / "styles" / "red.json").is_file()
    window.disconnect_canvas()


def test_cancelled_listing_asks_nothing(iface, server, catalog, tmp_path, monkeypatch):
    # The walk returns a partial plan when it is cancelled. The window must not
    # offer that partial plan for download.
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a: asked.append(a))
    window = open_window(iface, server, catalog)
    # Cancel during the first read, so the walk returns a partial plan.
    release = _start_held_listing(window, monkeypatch)
    QgsApplication.taskManager().cancelAll()
    release.set()
    wait_for(lambda: window.tasks.progress.isHidden() and not window.is_busy())
    for _ in range(20):
        QgsApplication.processEvents()
    assert asked == []
    assert not window.is_busy()
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
    wait_for(lambda: rows(window.registry_list.catalogs) == ["Test catalog"])
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
    assert (plugin.preprocessor is not None) is preprocessor
    project = QgsProject.instance()
    context = project.transformContext()
    extent = QgsRectangle(11.095, 44.0, 11.305, 45.0)
    planned = parquet_layer.plan(
        f"{catalog['base']}/points.parquet",
        context,
        parquet_layer.Limits(extent, QgsCoordinateReferenceSystem("EPSG:4326")),
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
    monkeypatch.setattr(widgets_module, "NODE_PAGE", 1)
    monkeypatch.setattr(registry_list_module, "CATALOG_PAGE", 2)
    window = open_window(iface, server, catalog)
    window.registry_list.catalogs.itemClicked.emit(window.registry_list.catalogs.item(0))
    wait_for(lambda: rows(window.catalog_tree.tree) == ["Test points", "Show 1 more of 1"])
    window.catalog_tree.tree.itemClicked.emit(window.catalog_tree.tree.topLevelItem(1), 0)
    assert rows(window.catalog_tree.tree) == ["Test points", "Missing"]
    # Five copies of the one registered catalog fill two pages and a half.
    window.registry_list.entries = window.registry_list.entries * 5
    assert rows(window.registry_list.catalogs)[2] == "Show 2 more of 3 publishers"
    window.registry_list.catalogs.itemClicked.emit(window.registry_list.catalogs.item(2))
    assert rows(window.registry_list.catalogs)[2:] == [
        "Test catalog",
        "Test catalog",
        "Show 1 more of 1 publisher",
    ]
    assert window.registry_list.shown.text() == "5 publishers"
    window.disconnect_canvas()


def test_window_layout_defaults_and_header(iface, server, catalog):
    window = open_window(iface, server, catalog)
    assert window.size() == window_module.DEFAULT_SIZE
    assert window.splitter.widget(0) is window.pages
    assert window.splitter.widget(1).widget().isAncestorOf(window.details.about_box)
    # The task bar spans both panes, below the splitter, and shows only
    # while a task runs.
    assert not window.splitter.isAncestorOf(window.tasks)
    assert window.tasks.isHidden()
    assert window.tagline.text() == "Open data registry"
    assert not window.logo.pixmap().isNull()
    # The browser link waits until the window can build its URL.
    assert window.details.open_browser.text() == "Open in Portolan browser \u2197"
    assert window.details.open_browser.isHidden()
    assert not window.refresh.isHidden()
    window.disconnect_canvas()


def test_window_restores_its_layout(iface, server, catalog):
    window = open_window(iface, server, catalog)
    window.show()
    # Qt 6 keeps a restored window inside the screen, which is 800 pixels
    # wide offscreen, so the size fits it.
    window.resize(720, 560)
    window.splitter.setSizes([300, 520])
    QgsApplication.processEvents()
    sizes = window.splitter.sizes()
    window.close()
    assert not QgsSettings().value(window_module.GEOMETRY_SETTING).isEmpty()
    window.disconnect_canvas()

    again = open_window(iface, server, catalog)
    again.show()
    QgsApplication.processEvents()
    assert again.size() == QSize(720, 560)
    assert again.splitter.sizes() == sizes
    again.close()
    again.disconnect_canvas()


def test_sections_collapse_and_remember(iface, server, catalog):
    window = open_window(iface, server, catalog)
    boxes = {
        "About": window.details.about_box,
        "Add to map": window.details.add_box,
        "All files": window.details.files_box,
        "Details": window.details.details_box,
    }
    for title, box in boxes.items():
        assert isinstance(box, QgsCollapsibleGroupBox)
        assert box.title() == title
        assert box.settingGroup() == window_module.SETTINGS_GROUP
        # The list of every file starts closed. The buttons cover most needs.
        assert box.isCollapsed() is (box is window.details.files_box)
    open_collection(window)
    window.show()
    QgsApplication.processEvents()
    window.details.details_box.setCollapsed(True)
    # A collapse hides the content, but the window's own choices stay.
    assert not window.details.facts.isVisible()
    assert not window.details.facts.isHidden()
    window.close()
    window.disconnect_canvas()

    again = open_window(iface, server, catalog)
    again.show()
    QgsApplication.processEvents()
    assert again.details.details_box.isCollapsed()
    assert not again.details.about_box.isCollapsed()
    again.details.details_box.setCollapsed(False)
    again.close()
    again.disconnect_canvas()


def test_hero_shows_thumbnail_or_extent(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_catalog(window)
    # The catalog has no thumbnail, so the hero draws its extent.
    wait_for(lambda: window.document is not None and window.document.kind == "catalog")
    window.details.hero.grab()
    assert window.details.hero.painted_extent
    window.catalog_tree.tree.setCurrentItem(window.catalog_tree.tree.topLevelItem(0))
    wait_for(lambda: window.details.hero.has_picture)
    # With a thumbnail, the hero draws the picture alone, with no inset map.
    window.details.hero.grab()
    assert not window.details.hero.painted_extent
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


# ---------- files, browser links, and check marks ----------


def test_pmtiles_shows_once(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    archive = f"{catalog['base']}/points.pmtiles"
    shown_hrefs = [
        window.details.assets.topLevelItem(i).data(0, ROLE).href
        for i in range(window.details.assets.topLevelItemCount())
    ]
    assert shown_hrefs.count(archive) == 1
    # The rows allow more than one choice, for Add selected and Download.
    assert (
        window.details.assets.selectionMode()
        == window.details.assets.SelectionMode.ExtendedSelection
    )
    window.disconnect_canvas()


def test_browser_links_of_the_open_node(iface, server, catalog, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    window = RegistryWindow(
        iface, server, f"{catalog['base']}/registry.json", "https://mirror.test/browse"
    )
    wait_for(lambda: rows(window.registry_list.catalogs) == ["Test catalog"])
    assert window.details.open_browser.isHidden()
    entry = window.registry_list.catalogs.item(0).data(ROLE)
    # The publisher row's context menu opens the catalog in the browser.
    (action,) = window.registry_list.catalog_menu(entry).actions()
    assert action.text() == "Open in Portolan browser \u2197"
    action.trigger()
    host = catalog["base"].removeprefix("http://")
    assert opened == [f"https://mirror.test/browse/#/external/http:/{host}/catalog.json"]

    open_collection(window)
    collection = f"{catalog['base']}/points/collection.json"
    link = f"https://mirror.test/browse/#/external/http:/{host}/points/collection.json"
    assert window.browser_link(collection) == link
    assert not window.details.open_browser.isHidden()
    window.details.open_browser.click()
    assert opened[-1] == link
    # Details link to the browser, the license, and the publisher's docs,
    # but not to the raw STAC document. Each is one row of the table.
    facts = window.details.facts.text()
    assert ">More<" in facts
    assert facts_module.BROWSER_LINK in facts and link in facts
    assert "STAC document" not in facts
    assert "<p>" not in facts
    window.disconnect_canvas()


def test_details_are_one_list_of_facts(iface, server, catalog):
    # The Philadelphia collections repeat a license link and title every
    # license link "License". Each fact shows once, as one row.
    window = open_window(iface, server, catalog)
    open_collection(window)
    href = f"{catalog['base']}/points/collection.json"
    raw = network.fetch_json(href)
    raw["license"] = "other"
    terms = "https://metadata.phila.gov/#help/help-faqs/what-are-the-terms-of-use/"
    portal = "https://opendataphilly.org/datasets/city-council-districts/"
    raw["links"] += [
        {"rel": "license", "href": portal, "title": "License"},
        {"rel": "describedby", "href": "./README.md", "title": "Human-readable documentation"},
        {"rel": "license", "href": terms, "title": "License"},
        {"rel": "license", "href": portal, "title": "License"},
        {"rel": "via", "href": "https://opendataphilly.org/datasets/land-use/"},
    ]
    window.show_document(read_document(raw, href))
    facts = window.details.facts.text()
    keys = [cell.split("</td>")[0].rsplit(">", 1)[-1] for cell in facts.split("<tr>")[1:]]
    assert keys == ["Publisher", "License", "Size", "Documentation", "Source", "More"]
    assert facts.count(">License<") == 1
    assert "Custom terms: " in facts
    assert facts.count(portal) == 1 and facts.count(html_escape(terms)) == 1
    assert ">opendataphilly.org<" in facts and ">metadata.phila.gov<" in facts
    assert ">Human-readable documentation<" in facts
    # The publisher's original page is the Source row, named by its host.
    assert 'opendataphilly.org/datasets/land-use/">opendataphilly.org<' in facts
    window.disconnect_canvas()


def test_the_browser_url_setting(iface, catalog):
    settings = QgsSettings()
    settings.setValue(REGISTRY_URL_SETTING, f"{catalog['base']}/registry.json")
    settings.setValue(BROWSER_URL_SETTING, "https://mirror.test/")
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    plugin.action.setChecked(True)
    try:
        link = plugin.window.browser_link(f"{catalog['base']}/catalog.json")
        assert link.startswith("https://mirror.test/#/external/http:/")
    finally:
        settings.remove(REGISTRY_URL_SETTING)
        settings.remove(BROWSER_URL_SETTING)
        plugin.unload()


def test_check_mark_after_add(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    row = asset_row(window, "geojson")
    assert not row.data(0, ON_MAP_ROLE)
    window.details.assets.clearSelection()
    row.setSelected(True)
    window.details.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1)
    assert row.data(0, ON_MAP_ROLE)
    assert "already reads this file" in row.toolTip(0)
    assert not asset_row(window, "relief").data(0, ON_MAP_ROLE)
    # A GeoParquet copy and the vector tiles keep their source in a property.
    window.details.view_button.click()
    window.details.load_button.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 3, timeout=60)
    assert asset_row(window, "Point tiles").data(0, ON_MAP_ROLE)
    assert asset_row(window, "data").data(0, ON_MAP_ROLE)
    # The mark goes when the layer goes.
    (geojson,) = QgsProject.instance().mapLayersByName("geojson")
    QgsProject.instance().removeMapLayer(geojson.id())
    assert not row.data(0, ON_MAP_ROLE)
    window.disconnect_canvas()


# ---------- scope ----------

# The envelope of this L shape keeps the 21 points with ids 10 to 30. The
# shape keeps the 11 points with ids 10 to 20.
L_SHAPE = (
    "POLYGON((11.095 44.0, 11.305 44.0, 11.305 44.1025, 11.205 44.1025, "
    "11.205 45.0, 11.095 45.0, 11.095 44.0))"
)


def _polygon_layer(wkt):
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "area", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromWkt(wkt))
    layer.dataProvider().addFeatures([feature])
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_scope_area_drives_a_parquet_add(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    area = _polygon_layer(L_SHAPE)
    window.details.scope_picker.choose(FROM_LAYER)
    window.details.scope_picker.layer_combo.setLayer(area)
    assert window.details.load_button.isEnabled()
    window.details.load_button.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 2, timeout=60)
    (layer,) = QgsProject.instance().mapLayersByName("points")
    assert sorted(f["id"] for f in layer.getFeatures()) == list(range(10, 21))
    assert layer.customProperty(parquet_layer.AREA_PROPERTY)
    window.disconnect_canvas()


def test_an_incomplete_scope_blocks_the_parquet_load(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.details.scope_picker.choose(FROM_LAYER)
    # No polygon layer is in the project, so the scope is not complete.
    assert not window.details.load_button.isEnabled()
    assert not window.details.download_all.isEnabled()
    assert window.details.scope_hint.text() == "Choose a polygon layer."
    assert not window.details.scope_hint.isHidden()
    # The vector tiles do not read the scope, so View on map still works.
    assert window.details.view_button.isEnabled()
    window.details.scope_picker.choose(EVERYTHING)
    assert window.details.load_button.isEnabled()
    assert window.details.scope_hint.isHidden()
    window.disconnect_canvas()


# ---------- COG mosaics ----------


def open_tiles(iface, server, catalog):
    window = RegistryWindow(iface, server, f"{catalog['base']}/tiles-registry.json")
    wait_for(lambda: rows(window.registry_list.catalogs) == ["Tiles catalog"])
    window.registry_list.catalogs.itemClicked.emit(window.registry_list.catalogs.item(0))
    wait_for(lambda: rows(window.catalog_tree.tree) == ["Test tiles"])
    collection = window.catalog_tree.tree.topLevelItem(0)
    collection.setExpanded(True)
    wait_for(lambda: collection.childCount() == 1 and collection.child(0).text(0) != "Reading…")
    return window, collection


def test_plain_parquet_is_a_table(iface, server, catalog):
    # Zoning Descriptions in the Philadelphia catalog is a lookup table. Its
    # columns name no geometry, so it is Parquet, not GeoParquet.
    window = open_window(iface, server, catalog)
    window.registry_list.catalogs.itemClicked.emit(window.registry_list.catalogs.item(0))
    href = f"{catalog['base']}/table/collection.json"
    raw = {
        "type": "Collection",
        "id": "plain",
        "title": "Plain table",
        "links": [],
        "table:columns": [
            {"name": "id", "type": "int64"},
            {"name": "name", "type": "string"},
            {"name": "score", "type": "double"},
        ],
        "assets": {
            "plain": {
                "href": "plain.parquet",
                "type": "application/vnd.apache.parquet",
                "roles": ["data"],
            }
        },
    }
    window.show_document(read_document(raw, href))
    row = asset_row(window, "plain")
    assert row.data(0, BADGE_ROLE)[0] == "Parquet"
    assert window.details.load_button.text() == "Load table"
    assert loadable.TABLE_TIP in window.details.load_button.toolTip()
    assert "plain (Parquet)" in window.details.load_button.toolTip()
    # A table has no geometry, so no area applies to it.
    assert window.details.scope_row.isHidden()
    window.details.load_button.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    (layer,) = QgsProject.instance().mapLayers().values()
    assert not layer.isSpatial()
    assert layer.featureCount() == 30
    window.disconnect_canvas()


def test_item_collection_adds_one_mosaic(iface, server, catalog):
    window, collection = open_tiles(iface, server, catalog)
    # The two items show as one row.
    assert collection.child(0).text(0) == "2 files"
    window.catalog_tree.tree.setCurrentItem(collection.child(0))
    wait_for(lambda: window.document is not None)
    assert window.document.title == "Test tiles"
    assert not window.details.mosaic_button.isHidden()
    assert window.details.view_button.isHidden() and window.details.load_button.isHidden()
    assert "Add as one layer" in window.details.no_assets.text()
    # The map view covers item a only.
    iface.canvas.setExtent(QgsRectangle(10.9, 43.9, 11.5, 44.7))
    window.details.scope_picker.choose(MAP_VIEW)
    window.details.mosaic_button.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    (layer,) = QgsProject.instance().mapLayers().values()
    assert layer.name() == "Test tiles"
    assert mosaic.sources(layer) == [f"{catalog['base']}/tiles/a/a.tif"]
    assert layer.isValid()
    window.disconnect_canvas()


def open_item_files(iface, server, catalog):
    """Open the "2 files" row of the tiles catalog, and wait until a mosaic can start."""
    window, collection = open_tiles(iface, server, catalog)
    window.catalog_tree.tree.setCurrentItem(collection.child(0))
    wait_for(lambda: window.document is not None and window.details.mosaic_button.isEnabled())
    return window


def _check_waits(window, iface, reason):
    """Check that the long actions wait while a task runs, and say ``reason``."""
    assert window.is_busy()
    assert not window.details.mosaic_button.isEnabled()
    assert not window.details.download_all.isEnabled()
    assert not window.details.download_selected.isEnabled()
    # A direct call does nothing either.
    before = window.tasks.progress.format()
    window.add_mosaic()
    window.start_download()
    assert window.tasks.progress.format() == before
    assert any(reason in item.text() for item in iface.bar.items())


def test_one_task_uses_the_progress_bar_at_a_time(iface, server, catalog, asked, monkeypatch):
    # The mosaic search, a download listing, and a download share the
    # progress bar and the Cancel button. Before, a mosaic search during a
    # download hid the Cancel button of the download.
    window = open_item_files(iface, server, catalog)
    searching = _held_search(monkeypatch)
    window.add_mosaic()
    wait_for(searching.is_set)
    _check_waits(window, iface, "Wait for the mosaic to finish")
    window.tasks.cancel.click()
    wait_for(lambda: not window.is_busy(), timeout=60)

    reading, release = _held_fetch(monkeypatch)
    window.start_download()
    wait_for(reading.is_set)
    _check_waits(window, iface, "Wait for the list of files to download to finish")
    window.tasks.cancel.click()
    release.set()
    wait_for(lambda: not window.is_busy(), timeout=60)
    assert asked == []

    # The job waits until Cancel stops it.
    monkeypatch.setattr(DownloadJob, "start", lambda _job: None)
    wait_for(lambda: hasattr(asset_row(window, "a").data(0, ROLE), "asset"))
    asset_row(window, "a").setSelected(True)
    window.details.download_selected.trigger()
    _check_waits(window, iface, "Wait for the download to finish")
    window.tasks.cancel.click()
    wait_for(lambda: not window.is_busy())
    assert window.details.mosaic_button.isEnabled()
    window.disconnect_canvas()


def _held_search(monkeypatch):
    """Hold the mosaic search until Cancel stops it. The event tells that it started."""
    searching = threading.Event()

    def held(document, scope, fetch_json, executor=None, cancelled=None):
        searching.set()
        deadline = time.monotonic() + 10
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        return []

    monkeypatch.setattr(mosaic, "item_hrefs_in", held)
    return searching


def test_a_busy_window_says_why_a_click_waits(iface, server, catalog, monkeypatch):
    # Before, a click while a task ran did nothing and said nothing.
    window = open_item_files(iface, server, catalog)
    searching = _held_search(monkeypatch)
    window.add_mosaic()
    wait_for(searching.is_set)
    window.add_mosaic()
    assert any("Wait for the mosaic to finish" in item.text() for item in iface.bar.items())
    window.tasks.cancel.click()
    wait_for(lambda: not window.is_busy(), timeout=60)
    window.disconnect_canvas()


def test_a_failed_mosaic_search_does_not_leave_the_window_busy(iface, server, catalog, monkeypatch):
    # An error in the callback left the busy flag set, so every later
    # "Add as one layer" did nothing.
    window = open_item_files(iface, server, catalog)

    def broken(*_args):
        raise RuntimeError("broken on purpose")

    # The search ends, and its callback fails before the build starts.
    monkeypatch.setattr(loading_module, "clip_wkt", broken)
    window.details.mosaic_button.click()
    wait_for(lambda: not window.is_busy(), timeout=60)
    monkeypatch.undo()
    window.details.mosaic_button.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    window.disconnect_canvas()


def _held_fetch(monkeypatch):
    """Hold each ``fetch_json`` of the window until the test releases it."""
    real = downloads_module.fetch_json
    reading, release = threading.Event(), threading.Event()

    def held(url):
        reading.set()
        release.wait(10)
        return real(url)

    monkeypatch.setattr(downloads_module, "fetch_json", held)
    return reading, release


def _start_held_listing(window, monkeypatch):
    """Start the listing of the whole catalog, and hold its reads.

    The function returns once the first read waits. The reads go on when
    the test sets the returned event.
    """
    open_collection(window)
    window.catalog_tree.header.clicked.emit()
    wait_for(lambda: window.document is not None and window.document.kind == "catalog")
    reading, release = _held_fetch(monkeypatch)
    window.details.download_all.trigger()
    wait_for(reading.is_set)
    return release


def test_cancel_stops_a_mosaic_search(iface, server, catalog, monkeypatch):
    # Before, the busy message said to cancel the search, but the window
    # showed Cancel for a download only.
    window = open_item_files(iface, server, catalog)
    searching = _held_search(monkeypatch)
    window.details.mosaic_button.click()
    wait_for(searching.is_set)
    assert not window.tasks.cancel.isHidden()
    window.tasks.cancel.click()
    wait_for(lambda: not window.is_busy(), timeout=60)
    assert window.tasks.cancel.isHidden()
    assert any("Stopped the search" in item.text() for item in iface.bar.items())
    assert QgsProject.instance().mapLayers() == {}
    window.disconnect_canvas()


def test_cancel_stops_a_mosaic_build(iface, server, catalog, monkeypatch):
    window = open_item_files(iface, server, catalog)
    building = threading.Event()

    def held(hrefs, cancelled, clip=None):
        building.set()
        deadline = time.monotonic() + 10
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise mosaic.MosaicCancelledError("cancelled")

    monkeypatch.setattr(mosaic, "write_vrt", held)
    window.details.mosaic_button.click()
    wait_for(building.is_set, timeout=60)
    wait_for(lambda: not window.tasks.cancel.isHidden())
    window.tasks.cancel.click()
    wait_for(lambda: not window.is_busy(), timeout=60)
    assert window.tasks.cancel.isHidden()
    assert any("Stopped the mosaic" in item.text() for item in iface.bar.items())
    window.disconnect_canvas()


def test_cancel_stops_a_download_listing(iface, server, catalog, tmp_path, asked, monkeypatch):
    window = open_window(iface, server, catalog)
    release = _start_held_listing(window, monkeypatch)
    assert not window.tasks.cancel.isHidden()
    window.tasks.cancel.click()
    release.set()
    wait_for(lambda: not window.is_busy(), timeout=60)
    assert window.tasks.cancel.isHidden()
    assert asked == []
    assert any("Listing of" in item.text() for item in iface.bar.items())
    window.disconnect_canvas()


def test_a_failed_listing_does_not_leave_the_window_busy(
    iface, server, catalog, asked, monkeypatch
):
    # An error in the callback must not leave the listing flag set, or every
    # later download does nothing.
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.catalog_tree.header.clicked.emit()
    wait_for(lambda: window.document is not None and window.document.kind == "catalog")

    def broken(*_args):
        raise RuntimeError("broken on purpose")

    # The question that confirms the download fails.
    monkeypatch.setattr(QMessageBox, "question", broken)
    logged = []
    monkeypatch.setattr(network, "report_failure", logged.append)
    window.details.download_all.trigger()
    wait_for(lambda: logged, timeout=60)
    assert "broken on purpose" in logged[0]
    assert not window.is_busy()
    assert window.details.download_all.isEnabled()
    window.disconnect_canvas()


def test_no_button_is_the_default(iface, server, catalog):
    # In a dialog, an Enter in the tree would click a default button.
    window = open_window(iface, server, catalog)
    buttons = window.findChildren(QPushButton)
    assert buttons
    assert not any(button.autoDefault() or button.isDefault() for button in buttons)
    window.disconnect_canvas()


def test_large_mosaic_asks_first(iface, server, catalog, monkeypatch):
    asked = []

    def question(_parent, _title, text):
        asked.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(loading_module, "CONFIRM_FILES", 1)
    monkeypatch.setattr(QMessageBox, "question", question)
    window, collection = open_tiles(iface, server, catalog)
    window.catalog_tree.tree.setCurrentItem(collection)
    wait_for(lambda: window.document is not None)
    window.details.mosaic_button.click()
    wait_for(lambda: asked and window.details.mosaic_button.isEnabled(), timeout=60)
    assert asked[0].startswith("2 files of Test tiles")
    assert QgsProject.instance().mapLayers() == {}
    window.disconnect_canvas()


@pytest.mark.parametrize("preprocessor", [True, False], ids=["preprocessor", "qgis-3.34"])
def test_saved_project_rebuilds_the_mosaic(iface, catalog, tmp_path, monkeypatch, preprocessor):
    if not preprocessor:
        monkeypatch.setattr(plugin_module, "PATH_PREPROCESSOR_VERSION", 10**9)
    elif Qgis.versionInt() < plugin_module.PATH_PREPROCESSOR_VERSION:
        pytest.skip("This QGIS cannot register a path preprocessor")
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    hrefs = [f"{catalog['base']}/tiles/{key}/{key}.tif" for key in ("a", "b")]
    layer = mosaic.build_mosaic(hrefs, "tiles")
    project = QgsProject.instance()
    project.addMapLayer(layer)
    first = Path(layer.source())
    saved = tmp_path / "saved.qgs"
    assert project.write(str(saved))
    project.clear()
    # The sweep deletes the VRT with its last layer.
    assert not first.exists()
    assert project.read(str(saved))
    (restored,) = project.mapLayers().values()
    wait_for(lambda: not mosaic.needs_rebuild(restored) and restored.isValid(), timeout=60)
    assert mosaic.sources(restored) == hrefs
    assert Path(restored.source()).is_file()
    assert restored.width() == 164
    plugin.unload()


# ---------- scoped downloads ----------


def test_download_keeps_the_items_in_the_scope(iface, server, catalog, tmp_path, asked):
    window, collection = open_tiles(iface, server, catalog)
    window.catalog_tree.tree.setCurrentItem(collection)
    wait_for(lambda: window.document is not None)
    iface.canvas.setExtent(QgsRectangle(10.9, 43.9, 11.5, 44.7))
    window.details.scope_picker.choose(MAP_VIEW)
    window.details.download_all.trigger()
    wait_for(
        lambda: asked and not window.is_busy() and not window.tasks.cancel.isVisible(), timeout=60
    )
    assert "1 item outside the chosen area" in asked[0]
    assert (tmp_path / "tiles" / "collection.json").is_file()
    assert (tmp_path / "tiles" / "a" / "a.tif").is_file()
    assert not (tmp_path / "tiles" / "b").exists()
    window.disconnect_canvas()


def test_download_clips_the_parquet_to_the_scope(iface, server, catalog, tmp_path, asked):
    window = open_window(iface, server, catalog)
    open_collection(window)
    area = _polygon_layer(L_SHAPE)
    window.details.scope_picker.choose(FROM_LAYER)
    window.details.scope_picker.layer_combo.setLayer(area)
    # Download in Add to map downloads everything in the area.
    window.details.download_all.trigger()
    clipped = tmp_path / "points.gpkg"
    wait_for(lambda: clipped.is_file() and not window.is_busy(), timeout=60)
    wait_for(lambda: any("Wrote 11 features" in item.text() for item in iface.bar.items()))
    assert "1 GeoParquet file downloads as GeoPackage" in asked[0]
    # The GeoJSON downloads whole. The GeoParquet does not download at all.
    assert (tmp_path / "points" / "points.geojson").is_file()
    assert not (tmp_path / "points" / "points.parquet").exists()
    copy = QgsVectorLayer(f"{clipped}|layername=features", "copy", "ogr")
    assert sorted(f["id"] for f in copy.getFeatures()) == list(range(10, 21))
    window.disconnect_canvas()


def test_item_collection_lists_its_files(iface, server, catalog, monkeypatch):
    monkeypatch.setattr(widgets_module, "NODE_PAGE", 1)
    window, collection = open_tiles(iface, server, catalog)
    window.catalog_tree.tree.setCurrentItem(collection.child(0))
    wait_for(lambda: window.document is not None)
    # One page of the item files, then a row that lists the rest. Add as one
    # layer covers every file, so no row stands for all of them.
    wait_for(lambda: rows(window.details.assets) == ["a", "Show 1 more of 1 file"])
    # The row holds the COG once the item is read.
    wait_for(lambda: hasattr(asset_row(window, "a").data(0, ROLE), "asset"))
    a = asset_row(window, "a")
    assert a.data(0, BADGE_ROLE)[:2] == ("COG", "cog")
    assert a.data(0, ROLE).href == f"{catalog['base']}/tiles/a/a.tif"
    # The explanation stays under the list, not in place of it.
    assert not window.details.no_assets.isHidden() and not window.details.assets.isHidden()
    window.details.assets.itemClicked.emit(window.details.assets.topLevelItem(1), 0)
    wait_for(lambda: rows(window.details.assets) == ["a", "b"])
    assert window.details.assets.selectedItems() == []
    assert not window.details.add.isEnabled()
    # A single item file adds as its own layer, and its row shows the mark.
    wait_for(lambda: hasattr(asset_row(window, "b").data(0, ROLE), "asset"))
    asset_row(window, "b").setSelected(True)
    assert window.details.add.isEnabled() and window.details.download_selected.isEnabled()
    window.details.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    assert asset_row(window, "b").data(0, ON_MAP_ROLE)
    window.disconnect_canvas()
