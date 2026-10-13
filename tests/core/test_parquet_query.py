from __future__ import annotations

import contextlib
import datetime
import json
import sqlite3

import pytest

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.geoparquet import plan_read
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
    ok, version = parquet_query.duckdb_status()
    assert ok
    assert version
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
    written = parquet_query.write_gpkg(con, points, plan, str(tmp_path / "all.gpkg"))
    assert written == parquet_query.Written(features=200, geometry_type="Point", refused=0)
    rows = _read_back(con, tmp_path / "all.gpkg")
    assert len(rows) == 200
    fid, ident, name, val, day, wkt = rows[0]
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
    assert parquet_query.write_gpkg(con, str(source), plan, str(target)).features == 1
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
    assert parquet_query.estimate(con, points, plan, box) == 21
    written = parquet_query.write_gpkg(con, points, plan, str(tmp_path / "box.gpkg"), box)
    assert written.features == 21
    assert sorted(row[1] for row in _read_back(con, tmp_path / "box.gpkg")) == list(range(10, 31))


def test_area_keeps_fewer_rows_than_its_envelope(con, points, tmp_path):
    plan = parquet_query.read_plan(con, points)
    # An L shape. Its right half starts above the points, so only x <= 11.205 keeps rows.
    area = (
        "POLYGON ((11.095 44, 11.205 44, 11.205 44.5, 11.305 44.5, "
        "11.305 45, 11.095 45, 11.095 44))"
    )
    box = (11.095, 44.0, 11.305, 45.0)
    # The estimate counts by the envelope, an upper bound.
    assert parquet_query.estimate(con, points, plan, box, area=area) == 21
    target = tmp_path / "area.gpkg"
    written = parquet_query.write_gpkg(con, points, plan, str(target), box, area=area)
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
    written = parquet_query.write_gpkg(con, str(source), plan, str(target), (4, 4, 6, 6), area=area)
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
    assert parquet_query.estimate(con, str(source), plan, (0, 0, 1, 1)) == rows
    target = tmp_path / "table.gpkg"
    written = parquet_query.write_gpkg(
        con, str(source), plan, str(target), (0, 0, 1, 1), srs="EPSG:4326"
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
    assert plan.covering is not None
    assert "bbox" not in [c.name for c in plan.columns]
    box = (11.095, 44.0, 11.305, 45.0)
    parquet_query.write_gpkg(con, points, plan, str(tmp_path / "cov.gpkg"), box)
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
    written = parquet_query.write_gpkg(con, str(source), plan, str(target), srs="EPSG:4326")
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
    written = parquet_query.write_gpkg(con, str(source), plan, str(tmp_path / "declared.gpkg"))
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
    parquet_query.write_gpkg(con, str(source), plan, str(target))
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
    assert parquet_query.write_gpkg(con, str(source), plan, str(target)).features == 2
    rows = con.execute("SELECT ogc_fid FROM ST_Read(?)", [str(target)]).fetchall()
    assert rows == [(5,), (5,)]


def test_z_survives_the_copy(con, tmp_path):
    source = tmp_path / "z.parquet"
    con.execute(
        f"COPY (SELECT ST_GeomFromText('POINT Z (1 2 3)') AS geometry) TO '{source}' (FORMAT parquet)"
    )
    plan = parquet_query.read_plan(con, str(source))
    target = tmp_path / "z.gpkg"
    parquet_query.write_gpkg(con, str(source), plan, str(target))
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
        parquet_query.write_gpkg(con, str(source), plan, str(target), cancelled=lambda: True)


def test_cancel_stops_a_read(con):
    import duckdb

    with (
        pytest.raises(duckdb.InterruptException),
        parquet_query._cursor(con, lambda: True) as cursor,
    ):
        cursor.execute("SELECT sum(a.range * b.range) FROM range(100000) a, range(100000) b")


def test_close_is_idempotent():
    parquet_query.close()
    parquet_query.close()
