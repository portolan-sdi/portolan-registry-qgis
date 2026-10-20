from __future__ import annotations

import contextlib
import datetime
import json
import sqlite3
import sys
import threading

import pytest

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.geoparquet import Clip, Covering, plan_read
from portolan_registry_qgis.core.parquet_query import Target
from portolan_registry_qgis.core.query import And, Compare, InList, Query, QueryError, Rows
from tests.points import write_points

pytest.importorskip("duckdb")


@pytest.fixture(scope="module")
def con():
    connection = parquet_query.connection()
    yield connection
    parquet_query.close()


@pytest.fixture(scope="module")
def points(tmp_path_factory, con):
    path = tmp_path_factory.mktemp("parquet") / "points.parquet"
    write_points(con, path, "'n' || i AS name, i * 1.5 AS val, DATE '2026-01-01' + i::INT AS day")
    return str(path)


def test_status_and_install_command(monkeypatch):
    import duckdb

    assert parquet_query.duckdb_status() == (True, duckdb.__version__)
    monkeypatch.setenv("FLATPAK_ID", "org.qgis.qgis")
    assert parquet_query.install_command(upgrade=False).startswith("flatpak run")
    assert "--upgrade" in parquet_query.install_command(upgrade=True)
    monkeypatch.delenv("FLATPAK_ID")
    monkeypatch.setattr(parquet_query.sys, "prefix", "/usr")
    assert "pip install --user 'duckdb>=1.5.0'" in parquet_query.install_command(upgrade=False)


def test_missing_duckdb_is_reported(monkeypatch):
    monkeypatch.setattr(parquet_query, "duckdb_status", lambda: (False, None))
    monkeypatch.setattr(parquet_query, "_connection", None)
    with pytest.raises(parquet_query.DuckDBMissingError):
        parquet_query.connection()


def test_absent_duckdb_has_no_version(monkeypatch):
    # A None entry in sys.modules makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "duckdb", None)
    assert parquet_query.duckdb_status() == (False, None)


def test_connection_is_shared():
    assert parquet_query.connection() is parquet_query.connection()


def test_extensions_fall_back_to_the_given_folder(monkeypatch, tmp_path):
    import duckdb

    loads = []

    def load(connection):
        loads.append(connection.execute("SELECT current_setting('extension_directory')").fetchone())
        if len(loads) == 1:
            raise duckdb.IOException("the default folder is read-only")

    monkeypatch.setattr(parquet_query, "_load_extensions", load)
    monkeypatch.setattr(parquet_query, "_connection", None)
    with pytest.raises(duckdb.IOException):
        parquet_query.connection()
    loads.clear()
    fallback = parquet_query.connection(str(tmp_path))
    try:
        assert loads[1] == (str(tmp_path),)
        assert fallback.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    finally:
        parquet_query.close()


@pytest.mark.parametrize(("flatpak", "client"), [(True, "httplib"), (False, "default")])
def test_the_flatpak_reads_with_httplib(monkeypatch, flatpak, client):
    monkeypatch.setattr(parquet_query, "is_flatpak", lambda: flatpak)
    parquet_query.close()
    try:
        con = parquet_query.connection()
        setting = "SELECT current_setting('httpfs_client_implementation')"
        assert con.execute(setting).fetchone() == (client,)
    finally:
        parquet_query.close()


def test_old_versions_are_rejected(monkeypatch):
    import duckdb

    monkeypatch.setattr(duckdb, "__version__", "1.4.9")
    assert parquet_query.duckdb_status() == (False, "1.4.9")
    monkeypatch.setattr(duckdb, "__version__", "1.5.0-dev123")
    assert parquet_query.duckdb_status() == (True, "1.5.0-dev123")


def _read_back(con, path):
    """Return the GeoPackage's rows. DuckDB reads it through its own GDAL."""
    return con.execute(
        "SELECT * EXCLUDE (geom) REPLACE (CAST(day AS VARCHAR) AS day), "
        "ST_AsText(geom) AS wkt FROM ST_Read(?)",
        [str(path)],
    ).fetchall()


def test_copy_all_rows(con, points, tmp_path):
    plan = parquet_query.read_plan(con, points)
    assert plan.geometry == "geometry"
    assert plan.geometry_type == "Point"
    assert parquet_query.estimate(con, points, plan) == 200
    written = parquet_query.write_gpkg(con, points, plan, Target(str(tmp_path / "all.gpkg")))
    assert written == parquet_query.Written(features=200, geometry_type="Point", refused=0)
    rows = _read_back(con, tmp_path / "all.gpkg")
    assert len(rows) == 200
    _fid, ident, name, val, day, wkt = rows[0]
    assert (ident, name, val) == (0, "n0", 0.0)
    assert day == "2026-01-01"
    assert wkt == "POINT (11 44)"


def test_dates_and_times_survive_the_copy(con, tmp_path):
    """Regression: the memory layer refused every Python date and datetime."""
    source = tmp_path / "dated.parquet"
    con.execute(
        f"""COPY (SELECT DATE '2020-01-02' AS day,
            TIMESTAMP '2020-01-01 03:04:05' AS seen,
            ST_Point(1, 2) AS geometry) TO '{source}' (FORMAT parquet)"""
    )
    plan = parquet_query.read_plan(con, str(source))
    target = tmp_path / "dated.gpkg"
    assert parquet_query.write_gpkg(con, str(source), plan, Target(str(target))).features == 1
    types = dict(
        con.execute(
            "SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM ST_Read(?))",
            [str(target)],
        ).fetchall()
    )
    assert types["day"] == "DATE"
    assert types["seen"].startswith("TIMESTAMP")
    day, seen = con.execute(
        "SELECT day, strftime(seen AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M:%S') FROM ST_Read(?)",
        [str(target)],
    ).fetchone()
    assert day == datetime.date(2020, 1, 2)
    assert seen == "2020-01-01 03:04:05"


def test_bbox(con, points, tmp_path):
    plan = parquet_query.read_plan(con, points)
    box = (11.095, 44.0, 11.305, 45.0)
    assert parquet_query.estimate(con, points, plan, Rows(Clip(box))) == 21
    written = parquet_query.write_gpkg(
        con, points, plan, Target(str(tmp_path / "box.gpkg"), Clip(box))
    )
    assert written.features == 21
    assert sorted(row[1] for row in _read_back(con, tmp_path / "box.gpkg")) == list(range(10, 31))


def test_box_estimate_needs_a_covering_column(con, tmp_path):
    source = tmp_path / "bare.parquet"
    con.execute(f"COPY (SELECT ST_Point(1, 2) AS geometry) TO '{source}' (FORMAT parquet)")
    plan = parquet_query.read_plan(con, str(source))
    assert plan.covering is None
    assert parquet_query.estimate(con, str(source), plan) == 1
    assert parquet_query.estimate(con, str(source), plan, Rows(Clip((0, 0, 5, 5)))) is None


def test_area_keeps_fewer_rows_than_its_envelope(con, points, tmp_path):
    plan = parquet_query.read_plan(con, points)
    # An L shape. Its right half starts above the points, so only x <= 11.205 keeps rows.
    area = (
        "POLYGON ((11.095 44, 11.205 44, 11.205 44.5, 11.305 44.5, "
        "11.305 45, 11.095 45, 11.095 44))"
    )
    box = (11.095, 44.0, 11.305, 45.0)
    # The estimate counts by the envelope, an upper bound.
    assert parquet_query.estimate(con, points, plan, Rows(Clip(box, area))) == 21
    target = tmp_path / "area.gpkg"
    written = parquet_query.write_gpkg(con, points, plan, Target(str(target), Clip(box, area)))
    assert written == parquet_query.Written(features=11, geometry_type="Point", refused=0)
    assert sorted(row[1] for row in _read_back(con, target)) == list(range(10, 21))


def test_area_picks_the_first_type_inside_it(con, tmp_path):
    source = tmp_path / "typed.parquet"
    con.execute(
        f"""COPY (SELECT ST_GeomFromText(w) AS geometry FROM (VALUES
            ('LINESTRING (0 0, 1 1)'), ('POINT (5 5)')) v(w)) TO '{source}' (FORMAT parquet)"""
    )
    plan = parquet_query.read_plan(con, str(source))
    assert plan.geometry_type == "Unknown"
    area = "POLYGON ((4 4, 6 4, 6 6, 4 4))"
    target = tmp_path / "typed.gpkg"
    written = parquet_query.write_gpkg(
        con, str(source), plan, Target(str(target), Clip((4, 4, 6, 6), area))
    )
    assert written == parquet_query.Written(features=1, geometry_type="MultiPoint", refused=0)


@pytest.mark.parametrize("rows", [5, 0])
def test_table_becomes_an_attribute_table(con, tmp_path, rows):
    source = tmp_path / "table.parquet"
    con.execute(
        f"""COPY (SELECT i AS id, 'n' || i AS name, 'x' AS geom FROM range({rows}) t(i))
        TO '{source}' (FORMAT parquet)"""
    )
    plan = parquet_query.read_plan(con, str(source))
    assert not plan.has_geometry
    assert parquet_query.estimate(con, str(source), plan, Rows(Clip((0, 0, 1, 1)))) == rows
    target = tmp_path / "table.gpkg"
    written = parquet_query.write_gpkg(
        con, str(source), plan, Target(str(target), Clip((0, 0, 1, 1)), srs="EPSG:4326")
    )
    assert written == parquet_query.Written(features=rows, geometry_type="None", refused=0)
    with contextlib.closing(sqlite3.connect(target)) as gpkg:
        assert gpkg.execute("SELECT table_name, data_type FROM gpkg_contents").fetchall() == [
            ("features", "attributes")
        ]
        assert gpkg.execute("SELECT count(*) FROM gpkg_geometry_columns").fetchone() == (0,)
    described = con.execute("DESCRIBE SELECT * FROM ST_Read(?)", [str(target)]).fetchall()
    # The attribute named geom keeps its name, because GDAL's geometry column is gone.
    assert [row[0] for row in described] == ["fid", "id", "name", "geom"]
    values = con.execute("SELECT id, name, geom FROM ST_Read(?) ORDER BY id", [str(target)])
    assert values.fetchall() == [(i, f"n{i}", "x") for i in range(rows)]


def test_covering_filter_gives_the_same_rows(con, points, tmp_path):
    schema = [(r[0], r[1]) for r in con.execute(f"DESCRIBE SELECT * FROM '{points}'").fetchall()]
    covering = {
        "columns": {
            "geometry": {
                "covering": {"bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}}
            }
        }
    }
    plan = plan_read(schema, json.dumps(covering))
    assert plan.covering == Covering(
        ("bbox", "xmin"), ("bbox", "ymin"), ("bbox", "xmax"), ("bbox", "ymax")
    )
    assert "bbox" not in [c.name for c in plan.columns]
    box = (11.095, 44.0, 11.305, 45.0)
    parquet_query.write_gpkg(con, points, plan, Target(str(tmp_path / "cov.gpkg"), Clip(box)))
    assert sorted(row[1] for row in _read_back(con, tmp_path / "cov.gpkg")) == list(range(10, 31))


def test_mixed_geometries_promote(con, tmp_path):
    source = tmp_path / "mixed.parquet"
    con.execute(
        f"""COPY (SELECT ST_GeomFromText(w) AS geometry, fid FROM (VALUES
            ('POINT (1 1)', 7), ('MULTIPOINT ((2 2), (3 3))', 8),
            ('LINESTRING (0 0, 1 1)', 9)) v(w, fid)) TO '{source}' (FORMAT parquet)"""
    )
    plan = parquet_query.read_plan(con, str(source))
    assert plan.geometry_type == "Unknown"
    target = tmp_path / "mixed.gpkg"
    written = parquet_query.write_gpkg(con, str(source), plan, Target(str(target), srs="EPSG:4326"))
    assert written == parquet_query.Written(features=3, geometry_type="MultiPoint", refused=1)
    rows = con.execute(
        "SELECT fid_1, fid, ST_AsText(geom) FROM ST_Read(?) ORDER BY fid", [str(target)]
    ).fetchall()
    # The attribute named fid keeps its values. The GeoPackage id gets another name.
    assert [row[1] for row in rows] == [7, 8, 9]
    assert rows[0][2] == "MULTIPOINT (1 1)"
    # The line keeps its attributes and has no geometry.
    assert rows[2][2] is None


def test_declared_type_counts_refused_features(con, tmp_path):
    """A file that declares Polygon but holds a MultiPolygon used to lose it silently."""
    source = tmp_path / "declared.parquet"
    con.execute(
        f"""COPY (SELECT ST_GeomFromText(w) AS geometry, i FROM (VALUES
            ('POLYGON ((0 0, 1 0, 1 1, 0 0))', 1),
            ('MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)))', 2)) v(w, i))
        TO '{source}' (FORMAT parquet)"""
    )
    schema = [("geometry", "GEOMETRY"), ("i", "INTEGER")]
    geo = {"columns": {"geometry": {"geometry_types": ["Polygon"]}}}
    plan = plan_read(schema, json.dumps(geo))
    written = parquet_query.write_gpkg(
        con, str(source), plan, Target(str(tmp_path / "declared.gpkg"))
    )
    assert written == parquet_query.Written(features=2, geometry_type="Polygon", refused=1)


def test_attribute_named_geom_survives(con, tmp_path):
    """GDAL names the geometry column geom, and dropped an attribute of that name."""
    source = tmp_path / "geom.parquet"
    con.execute(
        f"""COPY (SELECT 'label' AS geom, 1 AS geom_1, ST_Point(1, 2) AS geometry)
        TO '{source}' (FORMAT parquet)"""
    )
    plan = parquet_query.read_plan(con, str(source))
    target = tmp_path / "geom.gpkg"
    parquet_query.write_gpkg(con, str(source), plan, Target(str(target)))
    row = con.execute(
        "SELECT geom, geom_1, ST_AsText(geom_2) FROM ST_Read(?)", [str(target)]
    ).fetchone()
    assert row == ("label", 1, "POINT (1 2)")


def test_ogc_fid_column(con, tmp_path):
    """A column named OGC_FID, as in the Bologna Open Data files, used to stop the copy."""
    source = tmp_path / "ogc.parquet"
    con.execute(
        f"""COPY (SELECT 5 AS "OGC_FID", ST_Point(i, i) AS geometry FROM range(2) t(i))
        TO '{source}' (FORMAT parquet)"""
    )
    plan = parquet_query.read_plan(con, str(source))
    target = tmp_path / "ogc.gpkg"
    assert parquet_query.write_gpkg(con, str(source), plan, Target(str(target))).features == 2
    rows = con.execute("SELECT ogc_fid FROM ST_Read(?)", [str(target)]).fetchall()
    assert rows == [(5,), (5,)]


def test_z_survives_the_copy(con, tmp_path):
    source = tmp_path / "z.parquet"
    con.execute(
        f"COPY (SELECT ST_GeomFromText('POINT Z (1 2 3)') AS geometry) TO '{source}' (FORMAT parquet)"
    )
    plan = parquet_query.read_plan(con, str(source))
    target = tmp_path / "z.gpkg"
    parquet_query.write_gpkg(con, str(source), plan, Target(str(target)))
    wkt = con.execute("SELECT ST_AsText(geom) FROM ST_Read(?)", [str(target)]).fetchone()[0]
    assert wkt == "POINT Z (1 2 3)"


def test_cancel_stops_the_copy(con, tmp_path):
    import duckdb

    # Big enough that the copy runs for seconds, so the cancel lands mid-copy.
    source = tmp_path / "big.parquet"
    con.execute(
        f"COPY (SELECT ST_Point(i % 1000, i // 1000) AS geometry FROM range(2000000) t(i)) "
        f"TO '{source}' (FORMAT parquet)"
    )
    plan = parquet_query.read_plan(con, str(source))
    target = tmp_path / "cancel.gpkg"
    with pytest.raises(duckdb.InterruptException):
        parquet_query.write_gpkg(
            con, str(source), plan, Target(str(target)), parquet_query.Watch(lambda: True)
        )


def test_cancel_stops_a_read(con):
    import duckdb

    with (
        pytest.raises(duckdb.InterruptException),
        parquet_query.cursor(con, lambda: True) as cursor,
    ):
        cursor.execute("SELECT sum(a.range * b.range) FROM range(100000) a, range(100000) b")


class _Cursor:
    def __init__(self):
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True


def test_watcher_polls_until_the_query_ends():
    stop = threading.Event()
    polls = []

    def cancelled():
        polls.append(True)
        # The query ends while the watcher polls.
        stop.set()
        return False

    watched = _Cursor()
    parquet_query.interrupt_when(cancelled, watched, stop)
    assert polls == [True]
    assert not watched.interrupted


def test_watcher_interrupts_once_cancelled():
    watched = _Cursor()
    parquet_query.interrupt_when(lambda: True, watched, threading.Event())
    assert watched.interrupted


def test_watcher_reports_the_progress_duckdb_knows():
    stop = threading.Event()
    reported = []

    class Running(_Cursor):
        def __init__(self):
            super().__init__()
            self.values = iter([-1.0, 40.0, 100.0])

        def query_progress(self):
            value = next(self.values)
            if value == 100.0:
                stop.set()
            return value

    parquet_query.interrupt_when(None, Running(), stop, reported.append)
    assert reported == [0.4, 1.0]


def test_a_cursor_with_progress_turns_the_progress_bar_on(con):
    reported = []
    with parquet_query.cursor(con, progress=reported.append) as cur:
        setting = "SELECT current_setting('enable_progress_bar')"
        assert cur.execute(setting).fetchone() == (True,)
        assert cur.execute("SELECT current_setting('enable_progress_bar_print')").fetchone() == (
            False,
        )


def test_a_file_with_no_geometry_layer_is_left_alone(tmp_path):
    path = tmp_path / "empty.gpkg"
    with contextlib.closing(sqlite3.connect(path)) as gpkg:
        gpkg.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT)")
        gpkg.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
        gpkg.execute("INSERT INTO gpkg_contents VALUES ('features', 'features')")
        gpkg.commit()
    assert parquet_query.null_geometries(str(path)) == 0
    parquet_query.drop_geometry(str(path))
    with contextlib.closing(sqlite3.connect(path)) as gpkg:
        assert gpkg.execute("SELECT data_type FROM gpkg_contents").fetchall() == [("features",)]


def test_close_is_idempotent():
    parquet_query.close()
    parquet_query.close()


@pytest.fixture
def live():
    """The shared connection, opened again after ``test_close_is_idempotent`` closed it."""
    return parquet_query.connection()


def test_a_glob_and_a_list_read_as_one_table(live, tmp_path):
    con = live
    for part in ("a", "b"):
        folder = tmp_path / f"zone={part}"
        folder.mkdir()
        write_points(con, folder / "points.parquet", "'n' || i AS name")
    glob = str(tmp_path / "zone=*" / "*.parquet")
    files = parquet_query.list_files(con, glob)
    assert [path.rsplit("/", 2)[-2] for path in files] == ["zone=a", "zone=b"]
    plan = parquet_query.read_plan(con, glob)
    assert "zone" in [column.name for column in plan.columns]
    assert parquet_query.estimate(con, glob, plan) == 400
    listed = parquet_query.read_plan(con, files)
    assert parquet_query.estimate(con, files, listed) == 400


def test_a_dotted_bucket_gets_a_path_style_secret(live):
    con = live
    parquet_query.use_bucket(con, ["s3://us-west-2.data.test/a/*.parquet", "s3://plain/a.parquet"])
    parquet_query.use_bucket(con, "https://x.test/a.parquet")
    rows = con.execute(
        "SELECT name, scope FROM duckdb_secrets() WHERE name LIKE 'bucket_%'"
    ).fetchall()
    assert rows == [("bucket_us_west_2_data_test", ["s3://us-west-2.data.test"])]


@pytest.mark.parametrize(
    "source", ["https://x.test/a.parquet", ["https://x.test/a", "https://x.test/b"]]
)
def test_source_text_round_trips(source):
    assert parquet_query.source_from_text(parquet_query.source_text(source)) == source


_NO_BUCKET = (
    "HTTP Error: HTTP GET error reading 's3://parquetry/nfhl/state=' in region '' "
    "(HTTP 404 Not Found)\n\nNoSuchBucket: The specified bucket does not exist"
)


def test_read_error_names_a_bucket_that_amazon_does_not_have():
    message = parquet_query.read_error("s3://parquetry/nfhl/state=*/*.parquet", _NO_BUCKET)
    assert "parquetry" in message
    assert "endpoint" in message
    assert parquet_query.read_error(["s3://parquetry/a.parquet"], _NO_BUCKET) == message


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("s3://bucket/a.parquet", "HTTP 403 Forbidden"),
        ("https://x.test/a.parquet", _NO_BUCKET),
        (["https://x.test/a.parquet"], "IO Error"),
    ],
)
def test_read_error_keeps_other_errors(source, error):
    assert parquet_query.read_error(source, error) == error


def test_a_filter_and_two_columns_shape_the_copy(live, points, tmp_path):
    con = live
    plan = parquet_query.read_plan(con, points)
    rows = And(Compare("val", ">=", 30.0), InList("name", ("n20", "n21", "n90")))
    query = Query("filter", rows, ("name", "val"))
    assert parquet_query.estimate(con, points, plan, Rows(Clip((11, 44, 11.5, 44.2)), query)) == 2
    target = tmp_path / "query.gpkg"
    written = parquet_query.write_gpkg(con, points, plan, Target(str(target), query=query))
    assert written.features == 3
    described = con.execute("DESCRIBE SELECT * FROM ST_Read(?)", [str(target)]).fetchall()
    assert [row[0] for row in described] == ["fid", "name", "val", "geom"]


def test_custom_sql_replaces_the_generated_query(live, points, tmp_path):
    con = live
    plan = parquet_query.read_plan(con, points)
    sql = (
        f"SELECT id, ST_Buffer(geometry, 0.001) AS geometry FROM read_parquet('{points}') "
        "WHERE id < 7; -- seven rows"
    )
    query = Query(sql=sql)
    assert parquet_query.estimate(con, points, plan, Rows(query=query)) == 7
    target = tmp_path / "custom.gpkg"
    written = parquet_query.write_gpkg(con, points, plan, Target(str(target), query=query))
    assert written == parquet_query.Written(features=7, geometry_type="MultiPolygon", refused=0)


def test_custom_sql_must_return_the_geometry(live, points, tmp_path):
    con = live
    plan = parquet_query.read_plan(con, points)
    query = Query(sql=f"SELECT id FROM read_parquet('{points}')")
    with pytest.raises(QueryError, match="geometry"):
        parquet_query.write_gpkg(con, points, plan, Target(str(tmp_path / "x.gpkg"), query=query))
    assert not (tmp_path / "x.gpkg").exists()


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT 1; SELECT 2", "2 statements"),
        ("CREATE TABLE marker AS SELECT 1", "SELECT statement"),
        ("SELEC 1", "syntax error"),
        ("  ", "0 statements"),
    ],
)
def test_check_select_runs_nothing_it_refuses(live, points, tmp_path, sql, message):
    con = live
    plan = parquet_query.read_plan(con, points)
    with pytest.raises(QueryError, match=message):
        parquet_query.check_select(sql)
    with pytest.raises(QueryError, match=message):
        parquet_query.write_gpkg(
            con, points, plan, Target(str(tmp_path / "x.gpkg"), query=Query(sql=sql))
        )
    tables = con.execute("SELECT table_name FROM duckdb_tables() WHERE table_name = 'marker'")
    assert tables.fetchall() == []


def test_check_select_drops_the_final_semicolon():
    assert parquet_query.check_select("WITH t AS (SELECT 1 AS a) SELECT a FROM t;") == (
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t"
    )


def test_preview_shows_the_first_rows_with_geometry_types(live, points):
    con = live
    plan = parquet_query.read_plan(con, points)
    shown = parquet_query.preview(con, points, plan, Rows(query=Query(columns=("name",))))
    assert shown.columns == ("geometry", "name")
    assert len(shown.rows) == parquet_query.PREVIEW_ROWS
    assert shown.rows[0] == ("POINT", "n0")
    custom = parquet_query.preview(
        con, points, plan, Rows(query=Query(sql=f"SELECT id FROM read_parquet('{points}') LIMIT 3"))
    )
    assert custom == parquet_query.Preview(("id",), [(0,), (1,), (2,)])


def test_preview_stops_on_cancel(live, points):
    con = live
    import duckdb

    plan = parquet_query.read_plan(con, points)
    slow = Query(sql="SELECT i AS id FROM range(10000000000) t(i) ORDER BY random()")
    with pytest.raises(duckdb.InterruptException):
        parquet_query.preview(
            con, points, plan, Rows(query=slow), parquet_query.Watch(lambda: True)
        )


def test_copy_script_runs_in_a_fresh_duckdb(live, points, tmp_path):
    con = live
    import duckdb

    plan = parquet_query.read_plan(con, points)
    clip = Clip((11.095, 44.0, 11.305, 45.0))
    query = Query("filter", Compare("val", ">", 20.0), ("name",))
    target = tmp_path / "plugin.gpkg"
    written = parquet_query.write_gpkg(con, points, plan, Target(str(target), clip, query=query))
    script_target = tmp_path / "script.gpkg"
    script = parquet_query.copy_script(
        points, plan, str(script_target), Rows(clip, query), "EPSG:4326"
    )
    assert script.startswith("INSTALL spatial;\nLOAD spatial;\nINSTALL httpfs;\nLOAD httpfs;\n")
    with contextlib.closing(duckdb.connect()) as fresh:
        fresh.execute(script)
    with contextlib.closing(sqlite3.connect(script_target)) as gpkg:
        assert gpkg.execute("SELECT count(*) FROM features").fetchone() == (written.features,)


def test_copy_script_holds_the_bucket_secret():
    plan = plan_read([("id", "BIGINT")], None)
    script = parquet_query.copy_script("s3://a.b/f.parquet", plan, "out.gpkg")
    assert "CREATE SECRET IF NOT EXISTS bucket_a_b" in script
    assert "read_parquet('s3://a.b/f.parquet')" in script


def test_display_select_keeps_custom_sql():
    plan = plan_read([("id", "BIGINT")], None)
    assert parquet_query.display_select("f.parquet", plan, Rows(query=Query(sql=" SELECT 1 "))) == (
        "SELECT 1"
    )
    assert parquet_query.display_select("f.parquet", plan) == (
        'SELECT\n  "id" AS "id"\nFROM read_parquet(\'f.parquet\')'
    )


@pytest.mark.parametrize(
    ("sql", "kept"),
    [
        ("SELECT 1; -- end", "SELECT 1"),
        ("SELECT ';' AS a, \"b;\" FROM t;", "SELECT ';' AS a, \"b;\" FROM t"),
        ("SELECT 1 -- a; b\n /* c; */ ;", "SELECT 1 -- a; b\n /* c; */ "),
        ("SELECT 1", "SELECT 1"),
    ],
)
def test_without_terminator(sql, kept):
    assert parquet_query.without_terminator(sql) == kept


def test_copy_script_cuts_custom_sql_at_its_semicolon():
    plan = plan_read([("id", "BIGINT")], None)
    script = parquet_query.copy_script(
        "f.parquet", plan, "out.gpkg", Rows(query=Query(sql="SELECT 1 AS id; -- done"))
    )
    assert "COPY (\nSELECT 1 AS id\n) TO 'out.gpkg'" in script


def test_check_select_needs_duckdb(monkeypatch):
    monkeypatch.setattr(parquet_query, "duckdb_status", lambda: (False, None))
    with pytest.raises(parquet_query.DuckDBMissingError):
        parquet_query.check_select("SELECT 1")


def test_custom_sql_on_a_table_writes_a_table(live, tmp_path):
    con = live
    source = tmp_path / "table.parquet"
    con.execute(f"COPY (SELECT i AS id FROM range(6) t(i)) TO '{source}' (FORMAT parquet)")
    plan = parquet_query.read_plan(con, str(source))
    query = Query(sql=f"SELECT id * 10 AS tens FROM read_parquet('{source}') WHERE id < 4")
    target = tmp_path / "table.gpkg"
    written = parquet_query.write_gpkg(con, str(source), plan, Target(str(target), query=query))
    assert written == parquet_query.Written(features=4, geometry_type="None", refused=0)
    values = con.execute("SELECT tens FROM ST_Read(?) ORDER BY tens", [str(target)]).fetchall()
    assert values == [(0,), (10,), (20,), (30,)]


def test_preview_shows_a_wkb_geometry_as_its_type(live, tmp_path):
    con = live
    source = tmp_path / "wkb.parquet"
    con.execute(
        f"COPY (SELECT 1 AS id, ST_AsWKB(ST_Point(1, 2)) AS geometry) TO '{source}' (FORMAT parquet)"
    )
    plan = parquet_query.read_plan(con, str(source))
    assert plan.geometry_is_wkb_blob
    custom = Query(sql=f"SELECT id, geometry FROM read_parquet('{source}')")
    shown = parquet_query.preview(con, str(source), plan, Rows(query=custom))
    assert shown == parquet_query.Preview(("id", "geometry"), [(1, "POINT")])
