from __future__ import annotations

import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from portolan_registry_qgis.core import mirror, parquet_query
from portolan_registry_qgis.core.geoparquet import plan_read
from portolan_registry_qgis.core.scope import Everything, MapView
from portolan_registry_qgis.core.stac import Asset, read_document
from tests.tiles import EAST_AREA, TRIANGLE, WEST_VIEW

BASE = "https://x.test/tiles"
COG = "image/tiff; application=geotiff; profile=cloud-optimized"
STRUCT_BBOX = "STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE)"


def _collection(**extra):
    return read_document(
        {
            "type": "Collection",
            "id": "tiles",
            "links": [
                {"rel": "item", "href": "./a/a.json"},
                {"rel": "item", "href": "./b.json"},
                {"rel": "item", "href": "./a/again/a.json"},
                {"rel": "child", "href": "./sub/catalog.json"},
            ],
            **extra,
        },
        f"{BASE}/collection.json",
    )


def _asset(key, roles=("data",), fmt="cog"):
    return Asset(key, f"{BASE}/{key}.tif", None, None, roles, None, None, fmt)


def test_mirror_href():
    assets = {
        "data": {"href": "./x.parquet", "roles": ["data"]},
        "mirror": {"href": "./items.parquet", "roles": ["collection-mirror"]},
    }
    assert mirror.mirror_href(_collection(assets=assets)) == f"{BASE}/items.parquet"
    assert mirror.mirror_href(_collection()) is None


def test_item_documents_come_from_the_item_links():
    assert mirror.item_documents(_collection()) == {
        "a": f"{BASE}/a/a.json",
        "b": f"{BASE}/b.json",
    }


def test_cog_href_takes_the_first_cog_data_asset():
    thumbnail = _asset("thumb", roles=("thumbnail",))
    table = _asset("table", fmt="parquet")
    assert mirror.cog_href([thumbnail, table, _asset("a"), _asset("b")]) == f"{BASE}/a.tif"
    assert mirror.cog_href([thumbnail, table]) is None


def _plan(**types):
    return plan_read(list(types.items()), None)


def test_mirror_query_reads_a_list_bbox():
    plan = _plan(bbox="DOUBLE[]", assets="STRUCT(a STRUCT(href VARCHAR))")
    sql, params = mirror.mirror_query(plan, WEST_VIEW)
    assert "bbox[1]" in sql
    assert "links" not in sql
    assert "NULL, NULL FROM" in sql
    assert params == [10.9, 11.5, 43.9, 44.7]


def test_mirror_query_reads_a_struct_bbox():
    plan = _plan(id="VARCHAR", bbox=STRUCT_BBOX, assets="JSON", links="JSON")
    sql, params = mirror.mirror_query(plan, Everything())
    assert '"bbox"."xmin"' in sql
    assert 'to_json(links), "id"' in sql
    assert "WHERE" not in sql
    assert params == []
    # A table has no covering in its plan. The struct still counts.
    table = _plan(bbox=STRUCT_BBOX, assets="JSON")
    assert table.covering is None
    assert '"bbox"."ymax"' in mirror.mirror_query(table, Everything())[0]


def test_mirror_query_tests_the_area_against_the_geometry():
    plan = _plan(bbox=STRUCT_BBOX, assets="JSON", geometry="BLOB")
    sql, params = mirror.mirror_query(plan, EAST_AREA)
    assert 'ST_Intersects(ST_GeomFromWKB("geometry"), ST_GeomFromText(?))' in sql
    assert params[-1] == EAST_AREA.wkt
    # A map view tests the box only.
    assert "ST_Intersects" not in mirror.mirror_query(plan, WEST_VIEW)[0]
    # A box across the antimeridian keeps every row for the caller to test.
    across = MapView((170.0, -10.0, -170.0, 10.0))
    assert "WHERE" not in mirror.mirror_query(plan, across)[0]


def test_mirror_query_needs_bbox_and_assets():
    with pytest.raises(ValueError, match="no bbox or assets"):
        mirror.mirror_query(_plan(id="VARCHAR"), Everything())
    with pytest.raises(ValueError, match="no bbox or assets"):
        mirror.mirror_query(_plan(bbox="DOUBLE[]"), Everything())


@pytest.mark.parametrize(
    ("links", "expected"),
    [
        ([{"rel": "self", "href": "./a/a.json"}], f"{BASE}/a/a.json"),
        # The first self link with an href wins.
        ([{"rel": "self"}, {"rel": "self", "href": "b.json"}], f"{BASE}/b.json"),
        ([{"rel": "self", "href": ""}], None),
        ([{"rel": "parent", "href": "x.json"}, "x"], None),
        (None, None),
    ],
)
def test_self_href(links, expected):
    assert mirror._self_href(links, f"{BASE}/items.parquet") == expected


def test_self_href_that_cannot_resolve(monkeypatch):
    def refuse(_href, _base):
        raise ValueError("bad")

    monkeypatch.setattr(mirror, "asset_href", refuse)
    assert mirror._self_href([{"rel": "self", "href": "a.json"}], BASE) is None


def test_row_href():
    url = f"{BASE}/items.parquet"
    assets = json.dumps({"image": {"href": "./a.tif", "type": COG, "roles": ["data"]}})
    row = (11.0, 44.0, 11.64, 44.64, assets, "[]", "a")
    items = {"a": f"{BASE}/a/a.json"}
    assert mirror._row_href(row, url, Everything(), items) == f"{BASE}/a/a.tif"
    assert mirror._row_href(row, url, Everything(), {}) == f"{BASE}/a.tif"
    # A row out of the scope gives nothing. The query keeps rows across 180°.
    assert mirror._row_href(row, url, EAST_AREA, items) is None
    # A row with no bbox numbers and no ID stays, against the file.
    blank = (None, None, None, None, assets, "not json", None)
    assert mirror._row_href(blank, url, EAST_AREA, items) == f"{BASE}/a.tif"
    assert mirror._row_href((*row[:4], "[]", None, "a"), url, Everything(), items) is None


@pytest.fixture(scope="module")
def con():
    pytest.importorskip("duckdb")
    connection = parquet_query.connection()
    yield connection
    parquet_query.close()


@pytest.fixture(scope="module")
def served(tmp_path_factory, con):
    """Write the stac-geoparquet mirror of two COG items, as in the in-QGIS catalog.

    Item ``a`` has no ``self`` link, as in the GHSL mirror. Item ``b`` names
    its document in a ``self`` link relative to the file.
    """
    folder = tmp_path_factory.mktemp("tiles")
    rows = []
    for key, west in (("a", 11.0), ("b", 12.0)):
        south, east, north = 44.0, west + 0.64, 44.64
        links = "[]" if key == "a" else f"[{{'rel': 'self', 'href': './{key}/{key}.json'}}]"
        rows.append(
            f"SELECT '{key}' AS id, "
            f"{{'xmin': {west}, 'ymin': {south}, 'xmax': {east}, 'ymax': {north}}} AS bbox, "
            f"ST_MakeEnvelope({west}, {south}, {east}, {north}) AS geometry, "
            f"{{'thumbnail': {{'href': '../logo.png', 'type': 'image/png', 'roles': ['thumbnail']}}, "
            f"'image': {{'href': './{key}.tif', 'type': 'image/tiff; application=geotiff', "
            f"'roles': ['data']}}}} AS assets, "
            f"{links}::STRUCT(rel VARCHAR, href VARCHAR)[] AS links"
        )
    path = folder / "items.parquet"
    con.execute(f"COPY ({' UNION ALL '.join(rows)}) TO '{path}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT 1 AS id) TO '{folder / 'plain.parquet'}' (FORMAT parquet)")
    # An asset href must be HTTP to have a format, so DuckDB reads the file
    # from a server on loopback. DuckDB needs no range support.
    handler = functools.partial(_QuietHandler, directory=str(folder))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass


def test_mirror_hrefs(con, served):
    url = f"{served}/items.parquet"
    items = {"a": f"{served}/a/a.json", "b": f"{served}/b/b.json"}
    both = [f"{served}/a/a.tif", f"{served}/b/b.tif"]
    assert mirror.mirror_hrefs(con, url, Everything(), items=items) == both
    assert mirror.mirror_hrefs(con, url, WEST_VIEW, items=items) == both[:1]
    assert mirror.mirror_hrefs(con, url, EAST_AREA, items=items) == both[1:]
    # DuckDB tests the item geometry against the polygon, not its envelope.
    assert mirror.mirror_hrefs(con, url, TRIANGLE, items=items) == both[:1]


def test_a_mirror_without_self_links_resolves_against_the_items(con, served):
    # The GHSL mirror stores "./<file>.tif" relative to each item and no self
    # link. Resolved against the mirror file, the href gave HTTP 404.
    wrong = mirror.mirror_hrefs(con, f"{served}/items.parquet", Everything())
    assert wrong == [f"{served}/a.tif", f"{served}/b/b.tif"]


def test_a_file_that_is_not_stac_geoparquet(con, served):
    with pytest.raises(ValueError, match="no bbox or assets"):
        mirror.mirror_hrefs(con, f"{served}/plain.parquet", Everything())


def test_skipped_path():
    message = "Can't open /vsicurl/https://x.test/a.tif. Skipping it"
    assert mirror.skipped_path(message) == "/vsicurl/https://x.test/a.tif"
    assert mirror.skipped_path("Can't open /vsicurl/https://x.test/a.tif") is None
    assert mirror.skipped_path("Some other warning. Skipping it") is None
    assert mirror.skipped_path("") is None
