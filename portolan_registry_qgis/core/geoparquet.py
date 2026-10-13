"""Plan a DuckDB copy of a remote GeoParquet file into a GeoPackage.

The plan picks the geometry column, the CRS, the layer geometry type, and the
attribute types. It uses the GeoParquet ``geo`` metadata when the file has
it. The builders below write the count query and the ``COPY`` statement. A
bbox filter goes through the GeoParquet 1.1 ``covering`` column when the file
declares one, so DuckDB skips row groups through Parquet statistics instead
of scanning the whole file. A polygon filter adds an exact test after that
box filter.

A Parquet file with no geometry column is a table. Its plan copies the rows
into a GeoPackage attribute table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from portolan_registry_qgis.core.i18n import _

FieldKind = Literal["bool", "int", "double", "string", "date", "datetime"]

_INT_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
)
# GDAL holds at most a 64-bit integer, and refuses a DECIMAL wider than 19
# digits. These types go to the GeoPackage as DOUBLE.
_WIDE_TYPES = ("HUGEINT", "UHUGEINT")
_DOUBLE_TYPES = ("FLOAT", "DOUBLE", "REAL")
# The common names of a WKB geometry column, in lower case. The STAC table
# columns use the same names.
GEOMETRY_NAMES = frozenset({"geometry", "geom", "the_geom", "wkb_geometry", "shape", "geog"})
_MULTI = {
    "Point": "MultiPoint",
    "LineString": "MultiLineString",
    "Polygon": "MultiPolygon",
}
DEFAULT_CRS = "OGC:CRS84"
# The geometry type of a table. QGIS uses this name for a memory layer with
# no geometry.
NO_GEOMETRY = "None"  # i18n: skip - a QGIS geometry type
# The name of the one layer in each GeoPackage. A fixed name lets a saved
# project open a placeholder in place of a file that is gone.
LAYER = "features"


@dataclass(frozen=True)
class Column:
    """One attribute column and the QGIS field kind it maps to."""

    name: str
    duckdb_type: str
    kind: FieldKind


@dataclass(frozen=True)
class Covering:
    """The struct column and field names of a GeoParquet bbox covering."""

    xmin: tuple[str, ...]
    ymin: tuple[str, ...]
    xmax: tuple[str, ...]
    ymax: tuple[str, ...]


@dataclass(frozen=True)
class ReadPlan:
    """Everything needed to query a GeoParquet file and build a layer.

    A Parquet file with no geometry column is a table, as in the Portolan
    tabular collection. Its plan has no ``geometry``, the geometry type
    ``NO_GEOMETRY``, no CRS, and every column as an attribute.
    """

    geometry: str | None
    geometry_is_wkb_blob: bool
    crs: str
    geometry_type: str
    columns: tuple[Column, ...]
    covering: Covering | None

    @property
    def has_geometry(self) -> bool:
        """Whether the file has a geometry column. A table has none."""
        return self.geometry is not None


class GeoParquetError(ValueError):
    """The file is neither GeoParquet nor a table the plugin can read."""


def field_kind(duckdb_type: str) -> FieldKind:
    """Map a DuckDB column type to the QGIS field kind that holds it."""
    upper = duckdb_type.upper()
    if upper == "BOOLEAN":
        return "bool"
    if upper in _INT_TYPES:
        return "int"
    if upper in _DOUBLE_TYPES or upper in _WIDE_TYPES or upper.startswith("DECIMAL"):
        return "double"
    if upper == "DATE":
        return "date"
    if upper.startswith("TIMESTAMP"):
        return "datetime"
    return "string"


def quote(name: str) -> str:
    """Quote a SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _geo_metadata(raw: str | bytes | None) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _crs_input(column: dict[str, object]) -> str:
    """Return a string ``QgsCoordinateReferenceSystem.createFromUserInput`` reads.

    GeoParquet stores the CRS as PROJJSON. A missing ``crs`` key means
    OGC:CRS84. An ``id`` names the CRS directly. Anything else goes to PROJ as
    the PROJJSON text, which PROJ reads.
    """
    if "crs" not in column:
        return DEFAULT_CRS
    crs = column["crs"]
    if crs is None:
        return ""
    if isinstance(crs, str):
        return crs
    if isinstance(crs, dict):
        ident = crs.get("id")
        if isinstance(ident, dict) and "authority" in ident and "code" in ident:
            return f"{ident['authority']}:{ident['code']}"
        return json.dumps(crs)
    return DEFAULT_CRS


def layer_geometry_type(types: list[str]) -> str:
    """Return one QGIS memory-layer geometry type for the GeoParquet types.

    A layer holds one type, so single and multi parts of the same family
    promote to the multi type. Mixed families, or no declared types, return
    ``Unknown``. The caller then takes the type from the first feature.
    """
    has_z = any(name.endswith((" Z", " ZM")) for name in types)
    names = {name.split(" ")[0] for name in types}
    if len(names) == 1:
        family = names.pop()
    else:
        families = {_MULTI.get(name, name) for name in names}
        if len(families) != 1:
            return "Unknown"  # i18n: skip - a QGIS geometry type
        family = families.pop()
    return f"{family}Z" if has_z else family


def _covering(column: dict[str, object]) -> Covering | None:
    covering = column.get("covering")
    bbox = covering.get("bbox") if isinstance(covering, dict) else None
    if not isinstance(bbox, dict):
        return None
    parts = {}
    for key in ("xmin", "ymin", "xmax", "ymax"):
        path = bbox.get(key)
        if not isinstance(path, list) or not path or not all(isinstance(p, str) for p in path):
            return None
        parts[key] = tuple(path)
    return Covering(**parts)


def _implicit_covering(schema: list[tuple[str, str]]) -> Covering | None:
    """Find a ``bbox`` struct column that a file carries without covering metadata.

    GeoParquet 1.1 names this layout in its covering example, and writers
    such as DuckDB emit the column without the metadata that points to it.
    """
    for name, duckdb_type in schema:
        upper = duckdb_type.upper().replace(" ", "")
        if (
            name == "bbox"
            and upper.startswith("STRUCT(")
            and all(f"{key.upper()}" in upper for key in ("XMIN", "YMIN", "XMAX", "YMAX"))
        ):
            return Covering(("bbox", "xmin"), ("bbox", "ymin"), ("bbox", "xmax"), ("bbox", "ymax"))
    return None


def _pick_geometry(
    schema: list[tuple[str, str]], geo: dict[str, object]
) -> tuple[str, dict[str, object]] | None:
    columns = geo.get("columns")
    columns = columns if isinstance(columns, dict) else {}
    primary = geo.get("primary_column")
    names = [name for name, _ in schema]
    if isinstance(primary, str) and primary in names:
        info = columns.get(primary)
        return primary, info if isinstance(info, dict) else {}
    for name, duckdb_type in schema:
        if duckdb_type.upper().startswith("GEOMETRY"):
            info = columns.get(name)
            return name, info if isinstance(info, dict) else {}
    for name, duckdb_type in schema:
        if name.lower() in GEOMETRY_NAMES and duckdb_type.upper() == "BLOB":
            info = columns.get(name)
            return name, info if isinstance(info, dict) else {}
    return None


def _declares_geometry(geo: dict[str, object]) -> bool:
    """Return whether the ``geo`` metadata names a geometry column."""
    columns = geo.get("columns")
    return isinstance(geo.get("primary_column"), str) or (
        isinstance(columns, dict) and bool(columns)
    )


def _table_plan(schema: list[tuple[str, str]]) -> ReadPlan:
    return ReadPlan(
        geometry=None,
        geometry_is_wkb_blob=False,
        crs="",
        geometry_type=NO_GEOMETRY,
        columns=tuple(
            Column(name, duckdb_type, field_kind(duckdb_type)) for name, duckdb_type in schema
        ),
        covering=None,
    )


def plan_read(schema: list[tuple[str, str]], geo_json: str | bytes | None) -> ReadPlan:
    """Plan a read from the file's DuckDB schema and its ``geo`` metadata.

    A file with no geometry column and no ``geo`` metadata is a table. Its
    plan copies every column as an attribute.

    Args:
        schema: ``(name, type)`` pairs from ``DESCRIBE``.
        geo_json: The value of the ``geo`` key in the Parquet metadata, or
            None when the file has none.

    Raises:
        GeoParquetError: The file has no columns, or its ``geo`` metadata
            names a geometry column that the schema does not hold.
    """
    if not schema:
        raise GeoParquetError(_("The file has no columns"))
    geo = _geo_metadata(geo_json)
    picked = _pick_geometry(schema, geo)
    if picked is None:
        if _declares_geometry(geo):
            raise GeoParquetError(_("The file has no geometry column that its metadata names"))
        return _table_plan(schema)
    geometry, info = picked
    types = info.get("geometry_types")
    covering = _covering(info) or _implicit_covering(schema)
    skip = {geometry}
    if covering is not None:
        skip.add(covering.xmin[0])
    columns = tuple(
        Column(name, duckdb_type, field_kind(duckdb_type))
        for name, duckdb_type in schema
        if name not in skip
    )
    geometry_type = dict(schema)[geometry]
    return ReadPlan(
        geometry=geometry,
        geometry_is_wkb_blob=geometry_type.upper() == "BLOB",
        crs=_crs_input(info),
        geometry_type=layer_geometry_type(
            [t for t in types if isinstance(t, str)] if isinstance(types, list) else []
        ),
        columns=columns,
        covering=covering,
    )


def _path(parts: tuple[str, ...]) -> str:
    return ".".join(quote(part) for part in parts)


def literal(text: str) -> str:
    """Quote a SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


def _geometry(plan: ReadPlan) -> str:
    """Return the geometry column name.

    Raises:
        GeoParquetError: The plan is for a table, which has no geometry.
    """
    if plan.geometry is None:
        raise GeoParquetError(_("A table has no geometry column"))
    return plan.geometry


def shape(plan: ReadPlan) -> str:
    """Return the SQL expression for the geometry, as a DuckDB GEOMETRY."""
    geometry = quote(_geometry(plan))
    return f"ST_GeomFromWKB({geometry})" if plan.geometry_is_wkb_blob else geometry


def _check_area(bbox: tuple[float, float, float, float] | None, area: str | None) -> None:
    if area is not None and bbox is None:
        raise ValueError("An area needs its envelope as the bbox")  # i18n: skip - a caller bug


def _where(
    plan: ReadPlan,
    bbox: tuple[float, float, float, float] | None,
    *,
    exact: bool = True,
    area: str | None = None,
) -> tuple[str, list[object]]:
    """Return the WHERE clause and its parameters.

    Args:
        plan: The read plan.
        bbox: Keep only features that intersect this box, in the file's CRS.
        exact: Test each geometry against ``area``, else against ``bbox``.
            Without it, only the covering column filters, which reads far
            less of the file.
        area: Keep only features that intersect this Polygon or
            MultiPolygon WKT. ``bbox`` must be its envelope. The WKT goes
            to DuckDB as a parameter.
    """
    _check_area(bbox, area)
    where = [f"{quote(_geometry(plan))} IS NOT NULL"]
    params: list[object] = []
    if bbox is not None:
        west, south, east, north = bbox
        if plan.covering is not None:
            c = plan.covering
            where.append(
                f"{_path(c.xmin)} <= ? AND {_path(c.xmax)} >= ? "
                f"AND {_path(c.ymin)} <= ? AND {_path(c.ymax)} >= ?"
            )
            params += [east, west, north, south]
        if exact and area is not None:
            where.append(f"ST_Intersects({shape(plan)}, ST_GeomFromText(?))")
            params.append(area)
        elif exact:
            where.append(f"ST_Intersects({shape(plan)}, ST_MakeEnvelope(?, ?, ?, ?))")
            params += [west, south, east, north]
    return " AND ".join(where), params


def build_estimate(
    plan: ReadPlan,
    bbox: tuple[float, float, float, float] | None = None,
    area: str | None = None,
) -> tuple[str, list[object]] | None:
    """Build a query that estimates the number of features, with the URL first.

    Without a box, DuckDB answers from the Parquet footer. With a box, the
    query reads only the covering column, so the estimate counts the rows
    whose bbox overlaps the box. That is an upper bound of the features the
    copy keeps. With an area, the query still counts by its envelope
    ``bbox`` only, so the estimate stays an upper bound. A table has no
    geometry, so neither a box nor an area filters it, and the count is
    exact.

    Args:
        plan: The read plan.
        bbox: The box of the copy, in the file's CRS.
        area: The Polygon or MultiPolygon WKT of the copy. ``bbox`` must be
            its envelope.

    Returns:
        The SQL and its parameters after the URL, or None when a box is
        given and the file has no covering column. The estimate would then
        cost as much as the copy itself.

    Raises:
        ValueError: ``area`` is given without ``bbox``.
    """
    _check_area(bbox, area)
    if bbox is None or not plan.has_geometry:
        return "SELECT count(*) FROM read_parquet(?)", []
    if plan.covering is None:
        return None
    where, params = _where(plan, bbox, exact=False)
    return f"SELECT count(*) FROM read_parquet(?) WHERE {where}", params  # noqa: S608  # nosec B608 - identifiers are quoted, values are parameters


def build_first_type(
    plan: ReadPlan,
    bbox: tuple[float, float, float, float] | None = None,
    area: str | None = None,
) -> tuple[str, list[object]]:
    """Build a query for the type of the first geometry, with the URL first.

    The query returns the DuckDB type name, such as ``POLYGON``, and whether
    the geometry has Z. ``bbox`` and ``area`` filter as for ``build_copy``.

    Raises:
        GeoParquetError: The plan is for a table.
        ValueError: ``area`` is given without ``bbox``.
    """
    where, params = _where(plan, bbox, area=area)
    geometry = shape(plan)
    sql = (
        f"SELECT ST_GeometryType({geometry})::VARCHAR, ST_HasZ({geometry}) "  # noqa: S608  # nosec B608 - identifiers are quoted, values are parameters
        f"FROM read_parquet(?) WHERE {where} LIMIT 1"
    )
    return sql, params


def type_from_first(row: tuple[object, object] | None) -> str:
    """Return the layer geometry type for a file that declares none.

    A layer holds one geometry family. The first geometry picks it, promoted
    to its multi type so single and multi parts both fit. A file with no
    geometry in range gives ``Point``.
    """
    if row is None or not isinstance(row[0], str):
        return "Point"  # i18n: skip - a QGIS geometry type
    family = {name.upper(): name for name in (*_MULTI, *_MULTI.values(), "GeometryCollection")}.get(
        row[0].upper(),
        "Point",  # i18n: skip - a QGIS geometry type
    )
    family = _MULTI.get(family, family)
    return f"{family}Z" if row[1] else family


def _type_condition(plan: ReadPlan, layer_type: str) -> tuple[str, bool]:
    """Return the SQL test that a geometry fits ``layer_type``, and whether to promote it.

    A multi layer takes the single and the multi type of its family, and the
    copy promotes the single parts. A single layer takes only its own type.
    """
    family = layer_type.removesuffix("Z")
    singles = {multi: single for single, multi in _MULTI.items()}
    names = (singles[family], family) if family in singles else (family,)
    allowed = ", ".join(literal(name.upper()) for name in names)
    return f"ST_GeometryType({shape(plan)})::VARCHAR IN ({allowed})", family in singles


def _free_name(base: str, taken: set[str]) -> str:
    name, number = base, 1
    while name.casefold() in taken:
        name, number = f"{base}_{number}", number + 1
    return name


def fid_name(plan: ReadPlan) -> str:
    """Return a GeoPackage FID column name that no attribute uses.

    A GeoPackage keeps its feature id in a column named ``fid``. An
    attribute of that name with other values makes GDAL refuse the write.
    """
    return _free_name("fid", {column.name.casefold() for column in plan.columns})


def geometry_name(plan: ReadPlan) -> str:
    """Return a GeoPackage geometry column name that no attribute or the FID uses.

    GDAL names the geometry column ``geom``. It drops an attribute of the
    same name without an error.
    """
    taken = {column.name.casefold() for column in plan.columns}
    return _free_name("geom", taken | {fid_name(plan).casefold()})


def _value(column: Column) -> str:
    name = quote(column.name)
    if column.kind == "string":
        return f"CAST({name} AS VARCHAR)"
    if column.kind == "double" and column.duckdb_type.upper() not in _DOUBLE_TYPES:
        return f"CAST({name} AS DOUBLE)"
    return name


def output_name(name: str) -> str:
    """Return the GeoPackage name of an attribute column.

    GDAL's Arrow writer takes a column named exactly ``OGC_FID`` as the
    feature id and refuses the write. The lower-case name is an ordinary
    field, and SQLite matches column names without case anyway.
    """
    return "ogc_fid" if name == "OGC_FID" else name


def _gdal_options(creation: list[str]) -> list[str]:
    """Return the ``COPY`` options that write the one GeoPackage layer."""
    return [
        "FORMAT GDAL",
        "DRIVER 'GPKG'",
        f"LAYER_NAME {literal(LAYER)}",
        f"LAYER_CREATION_OPTIONS ({', '.join(literal(option) for option in creation)})",
    ]


def build_copy(
    plan: ReadPlan,
    path: str,
    layer_type: str,
    bbox: tuple[float, float, float, float] | None = None,
    srs: str | None = None,
    area: str | None = None,
) -> tuple[str, list[object]]:
    """Build the ``COPY`` of the features into a GeoPackage, with the URL first.

    A table copies every row and every column. DuckDB's GDAL writer always
    makes a layer with a geometry column, so for a table the column stays
    empty and has no spatial index. ``parquet_query.write_gpkg`` then turns
    the layer into an attribute table.

    Args:
        plan: The read plan.
        path: The GeoPackage to write.
        layer_type: The layer geometry type, from the plan or from
            ``type_from_first``. A feature of another type keeps its
            attributes and gets a NULL geometry. A table ignores it.
        bbox: Keep only features that intersect this box, in the file's CRS.
            A table ignores it.
        srs: The CRS to record in the GeoPackage, in any form GDAL reads.
            A table ignores it.
        area: Keep only features that intersect this Polygon or
            MultiPolygon WKT, in the file's CRS. ``bbox`` must be its
            envelope, so the covering column skips row groups first. A table
            ignores it.

    Returns:
        The SQL and its parameters after the URL.

    Raises:
        ValueError: ``area`` is given without ``bbox``.
    """
    _check_area(bbox, area)
    selected = [f"{_value(column)} AS {quote(output_name(column.name))}" for column in plan.columns]
    creation = [f"FID={fid_name(plan)}", f"GEOMETRY_NAME={geometry_name(plan)}"]
    if plan.geometry is None:
        creation.append("SPATIAL_INDEX=NO")
        return (
            f"COPY (SELECT {', '.join(selected)} FROM read_parquet(?)) "  # noqa: S608  # nosec B608 - identifiers are quoted
            f"TO {literal(path)} ({', '.join(_gdal_options(creation))})"
        ), []
    fits, promote = _type_condition(plan, layer_type)
    geometry = f"ST_Multi({shape(plan)})" if promote else shape(plan)
    selected.insert(0, f"CASE WHEN {fits} THEN {geometry} END AS {quote(plan.geometry)}")
    where, params = _where(plan, bbox, area=area)
    options = _gdal_options(creation)
    if not layer_type.endswith("Z"):
        # GDAL takes no Z types here. Without the option it reads the type
        # from the first feature, which gives an empty layer no geometry.
        options.append(f"GEOMETRY_TYPE {literal(layer_type.upper())}")
    if srs:
        options.append(f"SRS {literal(srs)}")
    sql = (
        f"COPY (SELECT {', '.join(selected)} FROM read_parquet(?) "  # noqa: S608  # nosec B608 - identifiers are quoted, values are parameters
        f"WHERE {where}) TO {literal(path)} ({', '.join(options)})"
    )
    return sql, params
