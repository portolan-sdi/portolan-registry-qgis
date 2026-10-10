from __future__ import annotations

import json

import pytest

from portolan_registry_qgis.core.geoparquet import (
    GeoParquetError,
    build_copy,
    build_estimate,
    fid_name,
    field_kind,
    layer_geometry_type,
    plan_read,
    type_from_first,
)

# Schema and geo metadata of a real GeoParquet 1.1 file:
# https://data.source.coop/nlebovits/phl-housing-demo/land_use/land_use.parquet
LAND_USE_SCHEMA = [
    ("objectid", "BIGINT"),
    ("c_dig1desc", "VARCHAR"),
    ("year", "SMALLINT"),
    ("Shape__Area", "DOUBLE"),
    ("bbox", "STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE)"),
    ("geometry", "GEOMETRY('OGC:CRS84')"),
]
LAND_USE_GEO = json.dumps(
    {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "bbox": [-75.28, 39.86, -74.95, 40.13],
                "geometry_types": ["MultiPolygon", "Polygon"],
                "covering": {
                    "bbox": {
                        "xmin": ["bbox", "xmin"],
                        "ymin": ["bbox", "ymin"],
                        "xmax": ["bbox", "xmax"],
                        "ymax": ["bbox", "ymax"],
                    }
                },
            }
        },
    }
)


def test_real_geoparquet_plan():
    plan = plan_read(LAND_USE_SCHEMA, LAND_USE_GEO)
    assert plan.geometry == "geometry"
    assert not plan.geometry_is_wkb_blob
    assert plan.crs == "OGC:CRS84"
    assert plan.geometry_type == "MultiPolygon"
    assert [c.name for c in plan.columns] == ["objectid", "c_dig1desc", "year", "Shape__Area"]
    assert [c.kind for c in plan.columns] == ["int", "string", "int", "double"]
    assert plan.covering.xmin == ("bbox", "xmin")


def test_copy_uses_the_covering_column():
    plan = plan_read(LAND_USE_SCHEMA, LAND_USE_GEO)
    sql, params = build_copy(plan, "/tmp/x.gpkg", "MultiPolygon", (-75.17, 39.94, -75.15, 39.96))
    assert '"bbox"."xmin" <= ?' in sql
    assert "ST_Intersects" in sql
    assert "LIMIT" not in sql
    assert params == [-75.15, -75.17, 39.96, 39.94, -75.17, 39.94, -75.15, 39.96]
    assert 'CAST("c_dig1desc" AS VARCHAR) AS "c_dig1desc"' in sql
    # The layer is multi, so single polygons are promoted and points are left out.
    assert 'ST_Multi("geometry") AS "geometry"' in sql
    assert "IN ('POLYGON', 'MULTIPOLYGON')" in sql
    assert "GEOMETRY_TYPE 'MULTIPOLYGON'" in sql
    assert "TO '/tmp/x.gpkg'" in sql


def test_copy_without_bbox_or_covering():
    plan = plan_read([("geom", "BLOB"), ("a", "INTEGER")], None)
    sql, params = build_copy(plan, "out.gpkg", "Point")
    assert plan.geometry_is_wkb_blob
    assert 'ST_GeomFromWKB("geom") AS "geom"' in sql
    assert "ST_Multi" not in sql
    assert "IN ('POINT')" in sql
    assert "ST_Intersects" not in sql
    assert params == []
    sql, params = build_copy(plan, "out.gpkg", "Point", (0, 0, 1, 1))
    assert "bbox" not in sql
    assert params == [0, 0, 1, 1]


def test_copy_options():
    plan = plan_read([("g", "GEOMETRY"), ("FID", "BIGINT"), ("n", "HUGEINT")], None)
    sql, _ = build_copy(plan, "it's.gpkg", "PolygonZ", srs="EPSG:2272")
    # GDAL takes no Z geometry type, so the option is left out for Z.
    assert "GEOMETRY_TYPE" not in sql
    assert "SRS 'EPSG:2272'" in sql
    assert "TO 'it''s.gpkg'" in sql
    assert "LAYER_CREATION_OPTIONS ('FID=fid_1')" in sql
    assert 'CAST("n" AS DOUBLE) AS "n"' in sql


def test_fid_name():
    assert fid_name(plan_read([("g", "GEOMETRY")], None)) == "fid"
    schema = [("g", "GEOMETRY"), ("fid", "INTEGER"), ("fid_1", "INTEGER")]
    assert fid_name(plan_read(schema, None)) == "fid_2"


def test_estimate():
    plan = plan_read(LAND_USE_SCHEMA, LAND_USE_GEO)
    sql, params = build_estimate(plan)
    assert sql == "SELECT count(*) FROM read_parquet(?)"
    assert params == []
    sql, params = build_estimate(plan, (0, 1, 2, 3))
    # Only the covering column filters, so DuckDB reads no geometry.
    assert '"bbox"."xmin" <= ?' in sql
    assert "ST_Intersects" not in sql
    assert params == [2, 0, 3, 1]
    without = plan_read([("geometry", "GEOMETRY")], None)
    assert build_estimate(without, (0, 1, 2, 3)) is None


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (("POINT", False), "MultiPoint"),
        (("MULTIPOLYGON", True), "MultiPolygonZ"),
        (("LINESTRING", False), "MultiLineString"),
        (("GEOMETRYCOLLECTION", False), "GeometryCollection"),
        (None, "Point"),
    ],
)
def test_type_from_first(row, expected):
    assert type_from_first(row) == expected


@pytest.mark.parametrize(
    ("schema", "geo", "geometry"),
    [
        # The geo metadata's primary column wins.
        ([("a", "GEOMETRY"), ("b", "GEOMETRY")], {"primary_column": "b"}, "b"),
        # A GEOMETRY column without metadata.
        ([("x", "INTEGER"), ("shape", "GEOMETRY('EPSG:3857')")], None, "shape"),
        # A WKB blob with a conventional name.
        ([("wkb_geometry", "BLOB")], None, "wkb_geometry"),
        # A primary column missing from the schema falls back to the type.
        ([("g", "GEOMETRY")], {"primary_column": "nope"}, "g"),
    ],
)
def test_geometry_column_choice(schema, geo, geometry):
    assert plan_read(schema, json.dumps(geo) if geo else None).geometry == geometry


def test_no_geometry_column():
    with pytest.raises(GeoParquetError):
        plan_read([("a", "INTEGER"), ("blob", "BLOB")], None)


@pytest.mark.parametrize(
    ("column", "crs"),
    [
        ({}, "OGC:CRS84"),
        ({"crs": None}, ""),
        ({"crs": {"id": {"authority": "EPSG", "code": 2272}}}, "EPSG:2272"),
        ({"crs": "EPSG:3857"}, "EPSG:3857"),
        (
            {"crs": {"type": "ProjectedCRS", "name": "custom"}},
            '{"type": "ProjectedCRS", "name": "custom"}',
        ),
        ({"crs": 42}, "OGC:CRS84"),
    ],
)
def test_crs(column, crs):
    geo = {"primary_column": "g", "columns": {"g": column}}
    assert plan_read([("g", "GEOMETRY")], json.dumps(geo)).crs == crs


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        (["Polygon"], "Polygon"),
        (["Polygon", "MultiPolygon"], "MultiPolygon"),
        (["Point Z"], "PointZ"),
        (["LineString", "MultiLineString Z"], "MultiLineStringZ"),
        (["Point", "Polygon"], "Unknown"),
        ([], "Unknown"),
    ],
)
def test_layer_geometry_type(types, expected):
    assert layer_geometry_type(types) == expected


@pytest.mark.parametrize(
    ("duckdb_type", "kind"),
    [
        ("BOOLEAN", "bool"),
        ("UBIGINT", "int"),
        ("HUGEINT", "double"),
        ("DECIMAL(21,1)", "double"),
        ("REAL", "double"),
        ("DATE", "date"),
        ("TIMESTAMP WITH TIME ZONE", "datetime"),
        ("VARCHAR", "string"),
        ("STRUCT(a INTEGER)", "string"),
        ("INTEGER[]", "string"),
    ],
)
def test_field_kind(duckdb_type, kind):
    assert field_kind(duckdb_type) == kind


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", b'{"columns": "nope"}'])
def test_bad_metadata_is_ignored(raw):
    assert plan_read([("geometry", "GEOMETRY")], raw).crs == "OGC:CRS84"


def test_partial_covering_is_ignored():
    geo = {"columns": {"g": {"covering": {"bbox": {"xmin": ["bbox", "xmin"]}}}}}
    assert plan_read([("g", "GEOMETRY")], json.dumps(geo)).covering is None


def test_quoting_resists_injection():
    plan = plan_read([('evil"; DROP TABLE x; --', "VARCHAR"), ("g", "GEOMETRY")], None)
    sql, _ = build_copy(plan, "x.gpkg", "Point")
    assert '"evil""; DROP TABLE x; --"' in sql


def test_bbox_struct_without_metadata_is_a_covering():
    schema = [
        ("id", "BIGINT"),
        ("geometry", "GEOMETRY('OGC:CRS84')"),
        ("bbox", "STRUCT(xmin DECIMAL(23,2), ymin DOUBLE, xmax DOUBLE, ymax DOUBLE)"),
    ]
    plan = plan_read(schema, None)
    assert plan.covering is not None
    assert [c.name for c in plan.columns] == ["id"]
    # A bbox column of another shape is an ordinary attribute.
    plan = plan_read([("geometry", "GEOMETRY"), ("bbox", "DOUBLE[]")], None)
    assert plan.covering is None
    assert [c.name for c in plan.columns] == ["bbox"]
