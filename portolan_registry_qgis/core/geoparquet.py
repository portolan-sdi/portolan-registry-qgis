"""Plan a DuckDB copy of a remote GeoParquet file into a GeoPackage.

A bbox filter uses the GeoParquet 1.1 ``covering`` column when the file has
one. A Parquet file with no geometry column is a table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from portolan_registry_qgis.core.i18n import _

if TYPE_CHECKING:
    from portolan_registry_qgis.core.stac import Bbox

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


@dataclass(frozen=True)
class Clip:
    """The part of a file that a copy keeps, in the file's CRS.

    No ``bbox`` keeps the whole file. An ``area`` needs ``bbox`` as its envelope,
    else ``ValueError``. A table ignores the clip.
    """

    bbox: Bbox | None = None
    area: str | None = None

    def __post_init__(self) -> None:
        """Check that an area comes with its envelope."""
        if self.area is not None and self.bbox is None:
            raise ValueError("An area needs its envelope as the bbox")  # i18n: skip - a caller bug

    def to_text(self) -> tuple[str, str]:
        """Return the box and the area as the text that a QGIS project saves.

        The box is ``west,south,east,north``. A part the clip does not have
        is an empty string.
        """
        bbox = ",".join(repr(value) for value in self.bbox) if self.bbox else ""
        return bbox, self.area or ""

    @classmethod
    def from_text(cls, bbox: str, area: str = "") -> Clip:
        """Read a clip from the text of ``to_text``.

        Raises:
            ValueError: A part of the box is not a number, or the area has no box.
        """
        parts = [float(part) for part in bbox.split(",")] if bbox else []
        box = (parts[0], parts[1], parts[2], parts[3]) if len(parts) == 4 else None
        return cls(box, area or None)


# The clip of a copy that keeps the whole file.
WHOLE_FILE = Clip()


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


def parse_json(value: object) -> object:
    """Parse JSON text, and return any other value as it is.

    Text that is not JSON gives None.
    """
    if not isinstance(value, (str, bytes)):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return None


def _geo_metadata(raw: str | bytes | None) -> dict[str, object]:
    value = parse_json(raw)
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


def implicit_covering(schema: list[tuple[str, str]]) -> Covering | None:
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


def _geometry_name(schema: list[tuple[str, str]], primary: object) -> str | None:
    """Return the name of the geometry column, or None when the file has none.

    The ``primary_column`` of the ``geo`` metadata wins. A GEOMETRY column
    comes next, then a BLOB column with a common geometry name.
    """
    if isinstance(primary, str) and any(name == primary for name, _ in schema):
        return primary
    for name, duckdb_type in schema:
        if duckdb_type.upper().startswith("GEOMETRY"):
            return name
    for name, duckdb_type in schema:
        if name.lower() in GEOMETRY_NAMES and duckdb_type.upper() == "BLOB":
            return name
    return None


def _pick_geometry(
    schema: list[tuple[str, str]], geo: dict[str, object]
) -> tuple[str, dict[str, object]] | None:
    name = _geometry_name(schema, geo.get("primary_column"))
    if name is None:
        return None
    columns = geo.get("columns")
    info = columns.get(name) if isinstance(columns, dict) else None
    return name, info if isinstance(info, dict) else {}


def _declares_geometry(geo: dict[str, object]) -> bool:
    """Return whether the ``geo`` metadata names a geometry column."""
    columns = geo.get("columns")
    return isinstance(geo.get("primary_column"), str) or (
        isinstance(columns, dict) and bool(columns)
    )


def _declared_types(info: dict[str, object]) -> list[str]:
    """Return the ``geometry_types`` that a geometry column's metadata lists."""
    types = info.get("geometry_types")
    return [t for t in types if isinstance(t, str)] if isinstance(types, list) else []


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

    Raises:
        GeoParquetError: The file has no columns, or ``geo`` names a column
            that the schema does not hold.
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
    covering = _covering(info) or implicit_covering(schema)
    skip = {geometry}
    if covering is not None:
        skip.add(covering.xmin[0])
    columns = tuple(
        Column(name, duckdb_type, field_kind(duckdb_type))
        for name, duckdb_type in schema
        if name not in skip
    )
    return ReadPlan(
        geometry=geometry,
        geometry_is_wkb_blob=dict(schema)[geometry].upper() == "BLOB",
        crs=_crs_input(info),
        geometry_type=layer_geometry_type(_declared_types(info)),
        columns=columns,
        covering=covering,
    )


def column_path(parts: tuple[str, ...]) -> str:
    """Return the SQL that reads a column, or a field of a struct column."""
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


def _where(plan: ReadPlan, clip: Clip, *, exact: bool = True) -> tuple[str, list[object]]:
    """Return the WHERE clause and its parameters.

    Without ``exact``, only the covering column filters, so DuckDB reads no geometry.
    The covering bounds are float literals, which DuckDB prunes row groups with fastest.
    """
    where = [f"{quote(_geometry(plan))} IS NOT NULL"] if exact else ["TRUE"]
    params: list[object] = []
    if clip.bbox is not None:
        west, south, east, north = (float(value) for value in clip.bbox)
        if plan.covering is not None:
            c = plan.covering
            where.append(
                f"{column_path(c.xmin)} <= {east!r} AND {column_path(c.xmax)} >= {west!r} "
                f"AND {column_path(c.ymin)} <= {north!r} AND {column_path(c.ymax)} >= {south!r}"
            )
        if exact and clip.area is not None:
            where.append(f"ST_Intersects({shape(plan)}, ST_GeomFromText(?))")
            params.append(clip.area)
        elif exact:
            where.append(f"ST_Intersects({shape(plan)}, ST_MakeEnvelope(?, ?, ?, ?))")
            params += [west, south, east, north]
    return " AND ".join(where), params


def build_estimate(plan: ReadPlan, clip: Clip = WHOLE_FILE) -> tuple[str, list[object]] | None:
    """Build a query that estimates the feature count, with the URL first.

    With a clip the count is an upper bound. Returns None when the clip has a
    box and the file has no covering column.
    """
    if clip.bbox is None or not plan.has_geometry:
        return "SELECT count(*) FROM read_parquet(?)", []
    if plan.covering is None:
        return None
    where, params = _where(plan, clip, exact=False)
    return f"SELECT count(*) FROM read_parquet(?) WHERE {where}", params  # noqa: S608  # nosec B608 - identifiers are quoted, values are parameters


def build_first_type(plan: ReadPlan, clip: Clip = WHOLE_FILE) -> tuple[str, list[object]]:
    """Build a query for the type and Z of the first geometry, with the URL first.

    Raises:
        GeoParquetError: The plan is for a table.
    """
    where, params = _where(plan, clip)
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


def build_select(
    plan: ReadPlan, layer_type: str, clip: Clip = WHOLE_FILE
) -> tuple[str, list[object]]:
    """Build the query of the features a copy writes, with the URL first.

    A feature not of ``layer_type`` gets a NULL geometry. A table selects every
    row, and ignores the clip.
    """
    selected = [f"{_value(column)} AS {quote(output_name(column.name))}" for column in plan.columns]
    if plan.geometry is None:
        return f"SELECT {', '.join(selected)} FROM read_parquet(?)", []  # noqa: S608  # nosec B608 - identifiers are quoted
    fits, promote = _type_condition(plan, layer_type)
    geometry = f"ST_Multi({shape(plan)})" if promote else shape(plan)
    selected.insert(0, f"CASE WHEN {fits} THEN {geometry} END AS {quote(plan.geometry)}")
    where, params = _where(plan, clip)
    sql = f"SELECT {', '.join(selected)} FROM read_parquet(?) WHERE {where}"  # noqa: S608  # nosec B608 - identifiers are quoted, values are parameters
    return sql, params


def build_write(
    plan: ReadPlan, query: str, path: str, layer_type: str, srs: str | None = None
) -> str:
    """Build the ``COPY`` of the rows of ``query`` into a GeoPackage at ``path``.

    A table gets no spatial index, and ignores the CRS.
    """
    creation = [f"FID={fid_name(plan)}", f"GEOMETRY_NAME={geometry_name(plan)}"]
    if plan.geometry is None:
        creation.append("SPATIAL_INDEX=NO")
        return f"COPY ({query}) TO {literal(path)} ({', '.join(_gdal_options(creation))})"
    options = _gdal_options(creation)
    if not layer_type.endswith("Z"):
        # GDAL takes no Z types here. Without the option it reads the type
        # from the first feature, which gives an empty layer no geometry.
        options.append(f"GEOMETRY_TYPE {literal(layer_type.upper())}")
    if srs:
        options.append(f"SRS {literal(srs)}")
    return f"COPY ({query}) TO {literal(path)} ({', '.join(options)})"


def build_copy(
    plan: ReadPlan,
    path: str,
    layer_type: str,
    clip: Clip = WHOLE_FILE,
    srs: str | None = None,
) -> tuple[str, list[object]]:
    """Build the ``COPY`` of the features into a GeoPackage in one statement, with the URL first."""
    query, params = build_select(plan, layer_type, clip)
    return build_write(plan, query, path, layer_type, srs), params
