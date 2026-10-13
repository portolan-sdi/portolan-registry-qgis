from __future__ import annotations

import json

import pytest

from portolan_registry_qgis.core.geoparquet import (
    NO_GEOMETRY,
    WHOLE_FILE,
    Clip,
    GeoParquetError,
    build_copy,
    build_estimate,
    build_first_type,
    fid_name,
    field_kind,
    geometry_name,
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
    sql, params = build_copy(
        plan, "/tmp/x.gpkg", "MultiPolygon", Clip((-75.17, 39.94, -75.15, 39.96))
    )
    assert '"bbox"."xmin" <= ?' in sql
    assert "ST_Intersects" in sql
    assert "LIMIT" not in sql
    assert params == [-75.15, -75.17, 39.96, 39.94, -75.17, 39.94, -75.15, 39.96]
    assert 'CAST("c_dig1desc" AS VARCHAR) AS "c_dig1desc"' in sql
    # The layer is multi, so single polygons are promoted. Points get a NULL geometry.
    assert "IN ('POLYGON', 'MULTIPOLYGON') THEN ST_Multi(\"geometry\") END AS" in sql
    assert "LAYER_NAME 'features'" in sql
    assert "GEOMETRY_TYPE 'MULTIPOLYGON'" in sql
    assert "TO '/tmp/x.gpkg'" in sql


def test_copy_without_bbox_or_covering():
    plan = plan_read([("geom", "BLOB"), ("a", "INTEGER")], None)
    sql, params = build_copy(plan, "out.gpkg", "Point")
    assert plan.geometry_is_wkb_blob
    assert 'THEN ST_GeomFromWKB("geom") END AS "geom"' in sql
    assert "ST_Multi" not in sql
    assert "IN ('POINT')" in sql
    assert "ST_Intersects" not in sql
    assert params == []
    sql, params = build_copy(plan, "out.gpkg", "Point", Clip((0, 0, 1, 1)))
    assert "bbox" not in sql
    assert params == [0, 0, 1, 1]


def test_copy_options():
    plan = plan_read([("g", "GEOMETRY"), ("FID", "BIGINT"), ("n", "HUGEINT")], None)
    sql, _ = build_copy(plan, "it's.gpkg", "PolygonZ", srs="EPSG:2272")
    # GDAL takes no Z geometry type, so the option is left out for Z.
    assert "GEOMETRY_TYPE" not in sql
    assert "SRS 'EPSG:2272'" in sql
    assert "TO 'it''s.gpkg'" in sql
    assert "LAYER_CREATION_OPTIONS ('FID=fid_1', 'GEOMETRY_NAME=geom')" in sql
    assert 'CAST("n" AS DOUBLE) AS "n"' in sql


def test_fid_name():
    assert fid_name(plan_read([("g", "GEOMETRY")], None)) == "fid"
    schema = [("g", "GEOMETRY"), ("fid", "INTEGER"), ("fid_1", "INTEGER")]
    assert fid_name(plan_read(schema, None)) == "fid_2"


def test_geometry_name():
    assert geometry_name(plan_read([("geometry", "GEOMETRY")], None)) == "geom"
    schema = [("geometry", "GEOMETRY"), ("GEOM", "VARCHAR"), ("geom_1", "INTEGER")]
    assert geometry_name(plan_read(schema, None)) == "geom_2"


def test_estimate():
    plan = plan_read(LAND_USE_SCHEMA, LAND_USE_GEO)
    sql, params = build_estimate(plan)
    assert sql == "SELECT count(*) FROM read_parquet(?)"
    assert params == []
    sql, params = build_estimate(plan, Clip((0, 1, 2, 3)))
    # Only the covering column filters, so DuckDB reads no geometry.
    assert '"bbox"."xmin" <= ?' in sql
    assert "ST_Intersects" not in sql
    assert params == [2, 0, 3, 1]
    without = plan_read([("geometry", "GEOMETRY")], None)
    assert build_estimate(without, Clip((0, 1, 2, 3))) is None


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
        # The same names that the STAC table columns accept.
        ([("id", "INTEGER"), ("shape", "BLOB")], None, "shape"),
        ([("GEOG", "BLOB")], None, "GEOG"),
        # A primary column missing from the schema falls back to the type.
        ([("g", "GEOMETRY")], {"primary_column": "nope"}, "g"),
    ],
)
def test_geometry_column_choice(schema, geo, geometry):
    assert plan_read(schema, json.dumps(geo) if geo else None).geometry == geometry


def test_no_geometry_column_is_a_table():
    plan = plan_read([("a", "INTEGER"), ("blob", "BLOB"), ("bbox", "DOUBLE[]")], None)
    assert not plan.has_geometry
    assert plan.geometry is None
    assert plan.geometry_type == NO_GEOMETRY
    assert plan.crs == ""
    assert plan.covering is None
    assert [c.name for c in plan.columns] == ["a", "blob", "bbox"]


@pytest.mark.parametrize(
    "geo",
    [
        {"primary_column": "geometry"},
        {"columns": {"geometry": {"encoding": "WKB"}}},
    ],
)
def test_metadata_without_its_geometry_column_is_an_error(geo):
    with pytest.raises(GeoParquetError, match="metadata"):
        plan_read([("a", "INTEGER")], json.dumps(geo))


def test_a_file_with_no_columns_is_an_error():
    with pytest.raises(GeoParquetError, match="no columns"):
        plan_read([], None)


def test_table_copy():
    plan = plan_read([("id", "BIGINT"), ("fid", "INTEGER"), ("geom", "VARCHAR")], None)
    sql, params = build_copy(plan, "t.gpkg", NO_GEOMETRY, Clip((0, 0, 1, 1)), "EPSG:4326")
    assert sql == (
        'COPY (SELECT "id" AS "id", "fid" AS "fid", CAST("geom" AS VARCHAR) AS "geom" '
        "FROM read_parquet(?)) TO 't.gpkg' (FORMAT GDAL, DRIVER 'GPKG', "
        "LAYER_NAME 'features', LAYER_CREATION_OPTIONS "
        "('FID=fid_1', 'GEOMETRY_NAME=geom_1', 'SPATIAL_INDEX=NO'))"
    )
    # A table has no location, so a box or an area does not filter it.
    assert params == []
    area = "POLYGON ((0 0, 1 0, 1 1, 0 0))"
    assert build_copy(plan, "t.gpkg", NO_GEOMETRY, Clip((0, 0, 1, 1), area)) == (sql, [])


def test_table_estimate_is_exact():
    plan = plan_read([("id", "BIGINT")], None)
    assert build_estimate(plan) == ("SELECT count(*) FROM read_parquet(?)", [])
    assert build_estimate(plan, Clip((0, 1, 2, 3))) == ("SELECT count(*) FROM read_parquet(?)", [])


def test_a_table_has_no_first_type():
    with pytest.raises(GeoParquetError):
        build_first_type(plan_read([("id", "BIGINT")], None))


AREA = "POLYGON ((-75.17 39.94, -75.15 39.94, -75.16 39.96, -75.17 39.94))"
AREA_BOX = (-75.17, 39.94, -75.15, 39.96)
AREA_CLIP = Clip(AREA_BOX, AREA)


def test_copy_in_an_area():
    plan = plan_read(LAND_USE_SCHEMA, LAND_USE_GEO)
    sql, params = build_copy(plan, "x.gpkg", "MultiPolygon", AREA_CLIP)
    # The covering column skips row groups by the envelope, then the polygon tests each geometry.
    assert '"bbox"."xmin" <= ?' in sql
    assert 'ST_Intersects("geometry", ST_GeomFromText(?))' in sql
    assert "ST_MakeEnvelope" not in sql
    # The WKT is a parameter, never part of the SQL.
    assert AREA not in sql
    assert params == [-75.15, -75.17, 39.96, 39.94, AREA]


def test_area_without_a_covering_column():
    plan = plan_read([("geom", "BLOB")], None)
    sql, params = build_copy(plan, "x.gpkg", "Point", AREA_CLIP)
    assert "bbox" not in sql
    assert 'ST_Intersects(ST_GeomFromWKB("geom"), ST_GeomFromText(?))' in sql
    assert params == [AREA]
    sql, params = build_first_type(plan, AREA_CLIP)
    assert "ST_GeomFromText(?)" in sql
    assert params == [AREA]


def test_estimate_in_an_area_counts_by_its_envelope():
    plan = plan_read(LAND_USE_SCHEMA, LAND_USE_GEO)
    assert build_estimate(plan, AREA_CLIP) == build_estimate(plan, Clip(AREA_BOX))
    without = plan_read([("geometry", "GEOMETRY")], None)
    assert build_estimate(without, AREA_CLIP) is None


def test_an_area_needs_its_envelope():
    with pytest.raises(ValueError, match="envelope"):
        Clip(area=AREA)
    with pytest.raises(ValueError, match="envelope"):
        Clip.from_text("", AREA)


@pytest.mark.parametrize(
    ("clip", "text"),
    [
        (WHOLE_FILE, ("", "")),
        (Clip((11.095, 44.0, 11.305, 45.0)), ("11.095,44.0,11.305,45.0", "")),
        (AREA_CLIP, ("-75.17,39.94,-75.15,39.96", AREA)),
    ],
)
def test_clip_text_round_trip(clip, text):
    assert clip.to_text() == text
    assert Clip.from_text(*text) == clip


def test_clip_reads_the_text_of_older_projects():
    """The two layer properties keep the format that earlier versions saved."""
    # 0.2.0 joined repr() of each float with commas, and saved the WKT as it was.
    saved = ",".join(repr(value) for value in AREA_BOX)
    assert Clip.from_text(saved, AREA) == AREA_CLIP
    assert Clip.from_text(saved) == Clip(AREA_BOX)
    assert Clip.from_text("") == WHOLE_FILE
    # A box without four numbers gives no box.
    assert Clip.from_text("1.0,2.0") == WHOLE_FILE
    with pytest.raises(ValueError):
        Clip.from_text("west,south,east,north")


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
