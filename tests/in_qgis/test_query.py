from __future__ import annotations

import html

import pytest
from qgis.core import QgsProject, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QToolButton

import portolan_registry_qgis
from portolan_registry_qgis.core.query import (
    And,
    Compare,
    InList,
    IsNull,
    Like,
    Not,
    Or,
    Query,
)
from portolan_registry_qgis.gui import sql_prompt
from portolan_registry_qgis.gui.scope import MAP_VIEW
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.filter_expression import FilterError, parse_filter

from .conftest import wait_for
from .test_gui import (
    _forget_layout,
    open_collection,
    open_tiles,
    open_window,
)

# The attribute columns of the points file, in file order.
POINT_COLUMNS = ["id", "name", "score", "day", "seen"]


@pytest.fixture(autouse=True)
def fresh_layout():
    _forget_layout()
    yield
    _forget_layout()


def open_sql(iface, server, catalog):
    """Open the points collection on the SQL tab, and wait for its columns."""
    window = open_window(iface, server, catalog)
    open_collection(window)
    window.details.add_tabs.setCurrentIndex(1)
    tab = window.details.sql_tab
    wait_for(lambda: tab.columns.count() == len(POINT_COLUMNS), timeout=60)
    return window, tab


def type_filter(tab, text):
    tab.filter.setExpression(text)
    tab.check_now()


def keep_columns(tab, names):
    for i in range(tab.columns.count()):
        on = tab.columns.itemText(i) in names
        tab.columns.setItemCheckState(i, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
    tab.check_now()
    # The dropdown writes its count on the next pass of the event loop.
    QApplication.processEvents()


def write_sql(tab, sql):
    tab.own_sql.setChecked(True)
    tab.sql.setText(sql)
    tab.check_now()


def points_sql(catalog, where):
    return (
        f"SELECT id, geometry FROM read_parquet('{catalog['base']}/points.parquet') WHERE {where}"
    )


# ---------- the tabs ----------


def test_add_to_map_has_a_load_tab_and_an_sql_tab(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    details = window.details
    tabs = details.add_tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["Load", "SQL"]
    assert tabs.currentIndex() == 0
    assert tabs.tabToolTip(1) == "View the DuckDB query used to load the layer."
    assert not details.load_button.isHidden()
    assert details.sql_tab.isHidden()
    # The SQL tab reads no columns until it opens.
    assert details.sql_tab.columns.count() == 0
    tabs.setCurrentIndex(1)
    assert details.load_tab.isHidden()
    assert not details.sql_tab.isHidden()
    wait_for(lambda: details.sql_tab.columns.count() == len(POINT_COLUMNS), timeout=60)
    assert details.sql_tab.columns.checkedItems() == POINT_COLUMNS
    QApplication.processEvents()
    assert details.sql_tab.columns.lineEdit().text() == "5 of 5 columns"
    window.disconnect_canvas()


def test_every_control_of_the_sql_tab_has_its_tooltip(iface, server, catalog):
    window, tab = open_sql(iface, server, catalog)
    assert tab.file.toolTip() == "Choose the GeoParquet file to query."
    assert tab.filter.toolTip().startswith("Filter rows with a QGIS expression")
    assert tab.filter.findChild(QToolButton).toolTip() == "Open the expression builder."
    assert tab.columns.toolTip() == "Select columns to include. Geometry is always included."
    assert tab.sql.toolTip() == "View the DuckDB query used to load the layer."
    assert tab.own_sql.toolTip() == (
        "Replace the row and column selections with a custom SELECT query."
    )
    assert tab.preview.toolTip() == (
        "Preview the first 100 matching rows without loading the layer."
    )
    assert tab.copy.toolTip() == "Copy a DuckDB command-line script to export the same layer."
    assert window.details.query_line.toolTip() == "Remove all row and column selections."
    window.disconnect_canvas()


def test_a_collection_without_geoparquet_has_no_tab_bar(iface, server, catalog):
    window, collection = open_tiles(iface, server, catalog)
    window.catalog_tree.tree.setCurrentItem(collection.child(0))
    wait_for(lambda: window.document is not None)
    assert window.details.add_tabs.isHidden()
    assert not window.details.load_tab.isHidden()
    window.disconnect_canvas()


# ---------- the choice ----------


def test_the_choice_shows_on_the_load_tab_and_shapes_the_load(iface, server, catalog):
    window, tab = open_sql(iface, server, catalog)
    details = window.details
    assert details.query_line.isHidden()
    type_filter(tab, '"id" < 10')
    keep_columns(tab, ["name"])
    assert tab.columns.lineEdit().text() == "1 of 5 columns"
    sql = tab.sql.text()
    assert 'AS "name"' in sql
    assert '"score"' not in sql
    assert '("id" < 10)' in sql
    assert not details.query_line.isHidden()
    expected = 'Loads 1 of 5 columns, only rows where "id" < 10.'
    assert details.query_line.text().startswith(html.escape(expected))
    assert details.load_button.text() == "Load data"
    assert details.load_button.toolTip().endswith(expected)
    details.add_tabs.setCurrentIndex(0)
    details.load_button.click()
    wait_for(lambda: QgsProject.instance().mapLayersByName("points"), timeout=60)
    (layer,) = QgsProject.instance().mapLayersByName("points")
    assert layer.featureCount() == 10
    assert [field.name() for field in layer.fields()] == ["fid", "name"]
    assert layer.customProperty(parquet_layer.FILTER_PROPERTY) == '"id" < 10'
    assert layer.customProperty(parquet_layer.COLUMNS_PROPERTY) == '["name"]'
    details.query_line.linkActivated.emit("clear")
    assert details.query_line.isHidden()
    assert details.load_button.text() == "Load full data"
    assert tab.filter.expression() == ""
    assert tab.columns.checkedItems() == POINT_COLUMNS
    window.disconnect_canvas()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("1; DROP TABLE x", "The filter does not parse"),
        ("upper(\"name\") = 'P1'", "Write my own SQL"),
        ('"nope" = 1', "The file has no column named nope"),
    ],
)
def test_a_bad_filter_shows_one_error_and_blocks_the_load(iface, server, catalog, text, message):
    window, tab = open_sql(iface, server, catalog)
    type_filter(tab, text)
    assert message in tab.filter_error.text()
    assert not tab.filter_error.isHidden()
    assert not tab.preview.isEnabled()
    assert not window.details.load_button.isEnabled()
    type_filter(tab, "")
    assert tab.filter_error.isHidden()
    assert window.details.load_button.isEnabled()
    window.disconnect_canvas()


def test_the_sql_follows_the_area_and_copies_as_a_script(iface, server, catalog):
    window, tab = open_sql(iface, server, catalog)
    assert tab.sql.isReadOnly()
    assert tab.sql.text().startswith("SELECT\n")
    assert f"read_parquet('{catalog['base']}/points.parquet')" in tab.sql.text()
    iface.canvas.setExtent(QgsRectangle(11.095, 44.0, 11.305, 45.0))
    window.details.scope_picker.choose(MAP_VIEW)
    assert "ST_MakeEnvelope" in tab.sql.text()
    tab.copy.click()
    copied = QApplication.clipboard().text()
    assert copied.startswith("INSTALL spatial;\nLOAD spatial;")
    assert "TO 'points.gpkg'" in copied
    assert tab.copy.text() == "Copied"
    window.disconnect_canvas()


def test_own_sql_replaces_the_choice_and_unchecking_restores_it(iface, server, catalog):
    window, tab = open_sql(iface, server, catalog)
    keep_columns(tab, ["id"])
    tab.own_sql.setChecked(True)
    assert not tab.sql.isReadOnly()
    assert not tab.filter.isEnabled()
    assert not tab.columns.isEnabled()
    write_sql(tab, "SELECT 1; SELECT 2")
    assert tab.sql_error.text() == "Write one SELECT statement. The SQL holds 2 statements."
    assert not window.details.load_button.isEnabled()
    write_sql(tab, points_sql(catalog, "id < 3"))
    assert tab.sql_error.isHidden()
    assert window.details.query_line.text().startswith(
        "Loads with custom SQL. The area of interest does not apply."
    )
    window.details.load_button.click()
    wait_for(lambda: QgsProject.instance().mapLayersByName("points"), timeout=60)
    (layer,) = QgsProject.instance().mapLayersByName("points")
    assert layer.featureCount() == 3
    assert "WHERE id < 3" in layer.customProperty(parquet_layer.SQL_PROPERTY)
    tab.own_sql.setChecked(False)
    assert tab.sql.isReadOnly()
    assert tab.columns.isEnabled()
    assert '"id" AS "id"' in tab.sql.text()
    window.disconnect_canvas()


def test_preview_shows_the_first_rows_under_the_sql(iface, server, catalog):
    window, tab = open_sql(iface, server, catalog)
    keep_columns(tab, ["id", "name"])
    assert tab.table.isHidden()
    tab.preview.click()
    wait_for(lambda: not tab.table.isHidden(), timeout=60)
    model = tab.table.model()
    assert model.rowCount() == 100
    headers = [model.horizontalHeaderItem(i).text() for i in range(model.columnCount())]
    assert headers == ["geometry", "id", "name"]
    assert model.item(0, 0).text() == "POINT"
    assert tab.footer.text() == "First 100 matching rows"
    assert QgsProject.instance().mapLayers() == {}
    window.disconnect_canvas()


def test_the_download_keeps_the_choice(iface, server, catalog, tmp_path, asked):
    window, tab = open_sql(iface, server, catalog)
    type_filter(tab, '"id" < 5')
    # The choice clips the GeoParquet even when the area is everything.
    window.details.download_all.trigger()
    clipped = tmp_path / "points.gpkg"
    wait_for(lambda: clipped.is_file() and not window.is_busy(), timeout=60)
    wait_for(lambda: any("Wrote 5 features" in item.text() for item in iface.bar.items()))
    copy = QgsVectorLayer(f"{clipped}|layername=features", "copy", "ogr")
    assert sorted(f["id"] for f in copy.getFeatures()) == list(range(5))
    window.disconnect_canvas()


# ---------- GDAL commands ----------


def test_the_mosaic_menu_copies_the_gdal_commands(iface, server, catalog):
    window, collection = open_tiles(iface, server, catalog)
    window.catalog_tree.tree.setCurrentItem(collection.child(0))
    wait_for(lambda: window.document is not None)
    details = window.details
    assert not details.mosaic_gdal.isHidden()
    assert details.mosaic_gdal.toolTip() == "Copy an equivalent GDAL command to run outside QGIS."
    assert details.copy_mosaic_gdal.text() == "Copy as GDAL command"
    iface.canvas.setExtent(QgsRectangle(10.9, 43.9, 11.5, 44.7))
    details.scope_picker.choose(MAP_VIEW)
    QApplication.clipboard().clear()
    details.copy_mosaic_gdal.trigger()
    wait_for(lambda: "gdalbuildvrt" in QApplication.clipboard().text(), timeout=60)
    text = QApplication.clipboard().text()
    assert f"/vsicurl/{catalog['base']}/tiles/a/a.tif" in text
    assert "/b.tif" not in text
    assert any("mosaic of 1 file" in item.text() for item in iface.bar.items())
    window.disconnect_canvas()


def test_geoparquet_has_no_gdal_load_menu(iface, server, catalog):
    window = open_window(iface, server, catalog)
    open_collection(window)
    # The load file is GeoParquet, so its command is the SQL on the SQL tab.
    assert window.details.load_gdal.isHidden()
    assert window.details.mosaic_gdal.isHidden()
    window.disconnect_canvas()


# ---------- saved projects ----------


def _saved_project(catalog, tmp_path, query):
    project = QgsProject.instance()
    planned = parquet_layer.plan(
        f"{catalog['base']}/points.parquet",
        project.transformContext(),
        parquet_layer.Limits(query=query),
    )
    layer, _ = parquet_layer.build(parquet_layer.prepare(planned), "points")
    project.addMapLayer(layer)
    saved = tmp_path / "saved.qgs"
    assert project.write(str(saved))
    project.clear()
    return saved


def test_a_saved_project_keeps_the_filter_and_columns(iface, catalog, tmp_path):
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    query = Query('"id" >= 190', Compare("id", ">=", 190), ("name",))
    saved = _saved_project(catalog, tmp_path, query)
    project = QgsProject.instance()
    assert project.read(str(saved))
    (restored,) = project.mapLayers().values()
    wait_for(lambda: restored.isValid() and restored.featureCount() == 10, timeout=60)
    assert [field.name() for field in restored.fields()] == ["fid", "name"]
    plugin.unload()


@pytest.mark.parametrize("accept", [True, False])
def test_saved_custom_sql_asks_before_it_runs(iface, catalog, tmp_path, monkeypatch, accept):
    asked = []

    def confirm(name, sql, _parent=None):
        asked.append((name, sql))
        return accept

    monkeypatch.setattr(sql_prompt, "confirm_sql", confirm)
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    sql = f"SELECT id, geometry FROM read_parquet('{catalog['base']}/points.parquet') WHERE id < 4"
    saved = _saved_project(catalog, tmp_path, Query(sql=sql))
    project = QgsProject.instance()
    assert project.read(str(saved))
    (restored,) = project.mapLayers().values()
    wait_for(lambda: asked, timeout=10)
    assert asked == [("points", sql)]
    if accept:
        wait_for(lambda: restored.isValid() and restored.featureCount() == 4, timeout=60)
    else:
        QApplication.processEvents()
        assert parquet_layer.needs_restore(restored)
    plugin.unload()


def test_the_prompt_shows_the_full_sql():
    prompt = sql_prompt.SqlPrompt("points", "SELECT 1 AS id")
    assert prompt.editor.text() == "SELECT 1 AS id"
    assert prompt.editor.isReadOnly()
    assert prompt.keep.isDefault()
    assert "read files on this computer" in prompt.warning.text()


# ---------- the filter ----------


@pytest.mark.parametrize(
    ("text", "condition"),
    [
        ("\"type\" = 'school'", Compare("type", "=", "school")),
        ("type = 'school'", Compare("type", "=", "school")),
        ('"e" <> -2.5', Compare("e", "<>", -2.5)),
        ("\"b\" NOT IN (1, 'x')", InList("b", (1, "x"), negated=True)),
        ("\"x\" ILIKE 'a%'", Like("x", "a%", ignore_case=True)),
        ("\"x\" NOT LIKE 'a%'", Like("x", "a%", negated=True)),
        ('"a" IS NOT NULL', IsNull("a", negated=True)),
        (
            '"a" = 1 OR NOT "b" IS NULL AND "c" > 2',
            Or(Compare("a", "=", 1), And(Not(IsNull("b")), Compare("c", ">", 2))),
        ),
    ],
)
def test_parse_filter_accepts_the_listed_forms(text, condition):
    assert parse_filter(text) == condition


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("1; DROP TABLE x", "does not parse"),
        ("upper(\"a\") = 'X'", "Write my own SQL"),
        ('"a" = "b"', "Write my own SQL"),
        ('"a" + 1 = 2', "Write my own SQL"),
        ('"a" = NULL', "IS NULL"),
        ('"a" LIKE 1', "Write my own SQL"),
    ],
)
def test_parse_filter_rejects_other_forms(text, message):
    with pytest.raises(FilterError, match=message):
        parse_filter(text)


def test_a_change_drops_the_old_preview_and_an_error_drops_the_sql(iface, server, catalog):
    window, tab = open_sql(iface, server, catalog)
    tab.preview.click()
    wait_for(lambda: not tab.table.isHidden(), timeout=60)
    type_filter(tab, "upper(\"name\") = 'P1'")
    assert tab.table.isHidden()
    assert tab.footer.isHidden()
    assert tab.sql.text() == ""
    type_filter(tab, '"id" < 3')
    assert '("id" < 3)' in tab.sql.text()
    window.disconnect_canvas()
