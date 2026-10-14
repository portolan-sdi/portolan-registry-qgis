from __future__ import annotations

import pytest

from portolan_registry_qgis.core import loadable
from portolan_registry_qgis.core.download import Plan, PlannedFile
from portolan_registry_qgis.core.loadable import (
    ButtonState,
    Targets,
    button_state,
    button_tip,
    files_hint,
    first,
    format_name,
    has_parquet,
    is_on_map,
    is_readable,
    is_table,
    item_cog,
    item_file,
    loadable_tip,
    only_items,
    parquet_name,
    parquet_to_clip,
    pick_targets,
    plan_summary,
    split_plan,
)
from portolan_registry_qgis.core.scope import Area, Everything, MapView
from portolan_registry_qgis.core.stac import Asset, Loadable, read_document

BASE = "https://data.test/points"
COG = "image/tiff; application=geotiff; profile=cloud-optimized"
PARQUET = "application/vnd.apache.parquet"
AREA = Area("POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))", (0, 0, 1, 1))


def _asset(href, fmt, **extra):
    fields = {"title": None, "type": None, "roles": ("data",), "size": None, "checksum": None}
    fields.update(extra)
    return Asset("key", href, format=fmt, **fields)


def _file(fmt, title="file", **extra):
    if fmt == "pmtiles":
        return Loadable(f"{BASE}/{title}.pmtiles", title, "pmtiles")
    href = f"{BASE}/{title}.{fmt}"
    return Loadable(href, title, fmt, asset=_asset(href, fmt, **extra))


def _collection(*, links=(), assets=None, columns=None, **extra):
    raw = {"type": "Collection", "id": "points", "title": "Points", "links": list(links)}
    raw["assets"] = assets or {}
    if columns is not None:
        raw["table:columns"] = columns
    raw.update(extra)
    return read_document(raw, f"{BASE}/collection.json")


def _children(*kinds):
    links = [
        {"rel": "item" if kind == "item" else "child", "href": f"./{i}/{kind}.json"}
        for i, kind in enumerate(kinds)
    ]
    return _collection(links=links)


def test_only_items():
    assert only_items(_children("item", "item"))
    assert not only_items(_children("item", "collection"))
    assert not only_items(_children())


def test_first_follows_the_order_of_the_formats():
    files = (_file("geojson"), _file("pmtiles"), _file("cog"))
    assert first(files, ("pmtiles", "cog")) is files[1]
    assert first(files, ("cog", "pmtiles")) is files[2]
    assert first(files, ("csv",)) is None


def test_item_cog():
    item = read_document(
        {
            "type": "Feature",
            "id": "a",
            "links": [],
            "assets": {
                "meta": {"href": "./a.json", "type": "application/json", "roles": ["metadata"]},
                "image": {"href": "./a.tif", "type": COG, "roles": ["data"]},
            },
        },
        f"{BASE}/a/a.json",
    )
    cog = item_cog(item)
    assert cog is not None
    assert cog.href == f"{BASE}/a/a.tif"
    assert item_cog(_collection()) is None


def test_is_table_and_format_name():
    table = _file("parquet", tabular=True)
    assert is_table(table)
    assert not is_table(_file("parquet"))
    assert not is_table(_file("pmtiles"))
    assert format_name(_file("pmtiles")) == "PMTiles"
    assert format_name(table) == "Parquet"
    assert format_name(_file("parquet")) == "GeoParquet"


def test_parquet_name():
    assert parquet_name(_asset(f"{BASE}/roads.parquet", "parquet")) == "roads"
    assert parquet_name(_asset(f"{BASE}/roads.parquet", "parquet", title="Roads")) == "Roads"


@pytest.mark.parametrize(
    ("formats", "duckdb_ok", "view", "load"),
    [
        (("pmtiles", "cog", "parquet", "geojson"), True, "pmtiles", "parquet"),
        # Without DuckDB, another format loads in place of the GeoParquet.
        (("cog", "parquet", "geojson"), False, "cog", "geojson"),
        # A document with GeoParquet alone keeps it, so the button can say
        # how to install DuckDB.
        (("parquet",), False, None, "parquet"),
        ((), True, None, None),
    ],
)
def test_pick_targets(formats, duckdb_ok, view, load):
    files = tuple(_file(fmt) for fmt in formats)
    targets = pick_targets(files, duckdb_ok=duckdb_ok)
    assert (targets.view.format if targets.view else None) == view
    assert (targets.load.format if targets.load else None) == load


def test_button_tip_leads_with_what_the_button_adds():
    view = button_tip(_file("pmtiles", "Point tiles"), view=True)
    assert view.split("\n") == [
        "Adds the vector tiles (PMTiles). They read only the tiles on screen.",
        loadable.VIEW_TIP,
        "File: Point tiles (PMTiles)",
    ]
    load = button_tip(_file("parquet", "data"), view=False)
    assert load.startswith("Loads the GeoParquet.")
    assert loadable.LOAD_TIP in load
    assert load.endswith("File: data (GeoParquet)")


def test_button_tip_of_a_table_and_of_an_unknown_format():
    table = button_tip(_file("parquet", "plain", tabular=True), view=False)
    assert table.split("\n") == [
        "Loads the Parquet table. It has no geometry, so it loads whole.",
        loadable.TABLE_TIP,
        "File: plain (Parquet)",
    ]
    # A format with no description leaves out the first line.
    other = button_tip(_file("geojson", "roads"), view=True)
    assert other.split("\n")[0] == loadable.VIEW_TIP


def test_has_parquet_skips_tables():
    assert has_parquet((_file("geojson"), _file("parquet")))
    assert not has_parquet((_file("parquet", tabular=True),))
    assert not has_parquet(())


def test_is_on_map():
    item = _file("cog", "relief")
    assert is_on_map(item, {item.href}, [])
    assert is_on_map(item, set(), [item.href])
    assert is_on_map(item, set(), [f"/vsicurl/{item.href}"])
    assert not is_on_map(item, {"https://data.test/other.tif"}, ["/tmp/other.gpkg"])


def test_button_state_of_a_document_with_both_buttons():
    document = _collection()
    targets = Targets(_file("pmtiles"), _file("parquet"))
    assert button_state(document, targets, selected=True, ready=True, busy=False) == ButtonState(
        add=True,
        view=True,
        load=True,
        mosaic=False,
        download_selected=True,
        download_all=True,
    )
    # An incomplete scope holds back GeoParquet and the download of the area.
    held = button_state(document, targets, selected=False, ready=False, busy=False)
    assert (held.add, held.load, held.download_all, held.view) == (False, False, False, True)
    # Another format does not read the scope.
    geojson = Targets(None, _file("geojson"))
    assert button_state(document, geojson, selected=False, ready=False, busy=False).load


def test_button_state_of_items_while_busy():
    items = _children("item", "item")
    targets = Targets(None, None)
    assert button_state(items, targets, selected=True, ready=True, busy=False).mosaic
    busy = button_state(items, targets, selected=True, ready=True, busy=True)
    assert busy == ButtonState(
        add=True,
        view=False,
        load=False,
        mosaic=False,
        download_selected=False,
        download_all=False,
    )
    nothing = button_state(None, targets, selected=False, ready=True, busy=False)
    assert not nothing.mosaic
    assert not nothing.download_all


def test_files_hint():
    assert files_hint(None, 0) == ""
    assert "Add as one layer" in files_hint(_children("item"), 3)
    assert files_hint(_collection(), 2) == ""
    assert "Choose a dataset in the tree" in files_hint(_children("collection"), 0)
    assert files_hint(_collection(), 0) == "This entry lists no files QGIS can open."


def test_parquet_to_clip_keeps_the_geoparquet_files():
    columns = [{"name": "id", "type": "int64"}]
    document = _collection(
        assets={
            "data": {"href": "./data.parquet", "type": PARQUET, "roles": ["data"]},
            "roads": {"href": "./roads.geojson", "type": "application/geo+json"},
        }
    )
    assert [asset.key for asset in parquet_to_clip(document, AREA)] == ["data"]
    assert [asset.key for asset in parquet_to_clip(document, MapView((0, 0, 1, 1)))] == ["data"]
    assert parquet_to_clip(document, Everything()) == []
    table = _collection(
        assets={"plain": {"href": "./plain.parquet", "type": PARQUET, "roles": ["data"]}},
        columns=columns,
    )
    assert parquet_to_clip(table, AREA) == []


def test_split_plan_leaves_out_the_clipped_files():
    clipped = _asset(f"{BASE}/data.parquet", "parquet")
    files = [PlannedFile(clipped.href, "data.parquet"), PlannedFile(f"{BASE}/a.json", "a.json")]
    original = Plan(files, [("x", "gone")], truncated=True, outside=2)
    whole = split_plan(original, [clipped])
    assert [entry.path for entry in whole.files] == ["a.json"]
    assert (whole.failures, whole.truncated, whole.outside) == ([("x", "gone")], True, 2)
    assert len(original.files) == 2


def test_plan_summary_of_a_plain_plan():
    plan = Plan([PlannedFile(f"{BASE}/a.json", "a.json", 2048)], [])
    assert plan_summary(plan) == "1 file, at least 2.0 KB."


def test_plan_summary_names_each_gap():
    files = [PlannedFile(f"{BASE}/{n}", n, 1000) for n in ("a", "b")]
    files.append(PlannedFile(f"{BASE}/c", "c"))
    plan = Plan(
        files, [("x", "gone"), ("y", "gone")], truncated=True, outside=1, unlisted=[("g", "why")]
    )
    assert plan_summary(plan, clipped=2).split("\n") == [
        "3 files, at least 2.0 KB.",
        "2 GeoParquet files download as GeoPackage, with the features in the area only.",
        "1 file does not declare a size.",
        "2 documents could not be read and are left out.",
        "The files of 1 partitioned dataset could not be listed and are left out.",
        "The listing stopped at its document limit.",
        "1 item outside the chosen area is left out.",
    ]


def test_loadable_tip():
    tiles = Loadable(f"{BASE}/a&b.pmtiles", "tiles", "pmtiles", layers=("roads", "<water>"))
    assert loadable_tip(tiles) == f"{BASE}/a&amp;b.pmtiles<br>Layers: roads, &lt;water&gt;"
    data = _file("parquet", roles=("data", "visual"))
    tip = loadable_tip(data, on_map=True)
    assert tip.split("<br>") == [
        data.href,
        "Roles: data, visual",
        "A layer in the project already reads this file.",
    ]
    assert loadable_tip(_file("geojson", roles=())) == f"{BASE}/file.geojson"


GLOB_BASE = "https://pub.test/points/collection.json"


def _glob_collection(href):
    raw = {
        "type": "Collection",
        "id": "points",
        "title": "Points",
        "links": [],
        "assets": {"data": {"href": "./zone=*/a*.parquet", "roles": ["data"]}},
    }
    return read_document(raw, href)


def test_an_https_glob_disables_load_full_data_and_says_why():
    document = _glob_collection(GLOB_BASE)
    targets = pick_targets(document.loadable(), duckdb_ok=True)
    assert targets.load is not None
    assert not is_readable(targets.load)
    state = button_state(document, targets, selected=False, ready=True, busy=False)
    assert not state.load
    tip = button_tip(targets.load, view=False)
    assert "https://pub.test/points/zone=*/a*.parquet" in tip
    assert "s3://" in tip
    assert parquet_to_clip(document, MapView((0, 0, 1, 1))) == []


def test_a_bucket_glob_loads_and_names_its_folder():
    document = _glob_collection("https://data.source.coop/acct/points/collection.json")
    targets = pick_targets(document.loadable(), duckdb_ok=True)
    assert targets.load is not None
    assert is_readable(targets.load)
    assert button_state(document, targets, selected=False, ready=True, busy=False).load
    assert "zone=*/a*.parquet" in button_tip(targets.load, view=False)
    asset = targets.load.asset
    assert asset is not None
    assert parquet_name(asset) == "points"
    assert is_on_map(targets.load, {asset.read_url}, [])
    assert parquet_to_clip(document, MapView((0, 0, 1, 1))) == [asset]


def test_item_file_takes_any_data_format():
    item = read_document(
        make_item_doc({"data": {"href": "./a.parquet", "roles": ["data"]}}),
        f"{BASE}/a/a.json",
    )
    found = item_file(item)
    assert found is not None
    assert found.format == "parquet"
    assert found.title == "Item a"
    tif = read_document(
        make_item_doc(
            {
                "data": {"href": "./a.parquet", "roles": ["data"]},
                "image": {"href": "./a.tif", "roles": ["data"]},
            }
        ),
        f"{BASE}/a/a.json",
    )
    assert item_file(tif).format == "cog"
    assert item_file(read_document(make_item_doc({}), f"{BASE}/a/a.json")) is None


def make_item_doc(assets):
    return {
        "type": "Feature",
        "id": "a",
        "properties": {"title": "Item a"},
        "geometry": None,
        "links": [],
        "assets": assets,
    }
