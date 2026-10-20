"""Copy GeoParquet over HTTP into a GeoPackage with DuckDB.

QGIS does not ship DuckDB, so every function imports it inside the function.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from portolan_registry_qgis.core.geoparquet import (
    NO_GEOMETRY,
    UNKNOWN,
    WHOLE_FILE,
    Clip,
    ReadPlan,
    build_estimate,
    build_first_type,
    build_select,
    build_write,
    literal,
    plan_read,
    quote,
    type_from_first,
)
from portolan_registry_qgis.core.hrefs import bucket_region
from portolan_registry_qgis.core.i18n import _, ngettext
from portolan_registry_qgis.core.query import (
    ALL_ROWS,
    DEFAULT_QUERY,
    Query,
    QueryError,
    Rows,
    inline,
    pretty,
    result_plan,
    selection,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    # Receives the share of a query that DuckDB has done, from 0 to 1.
    Progress = Callable[[float], None]

# What DuckDB reads: one URL, a bucket glob, or a list of URLs read as one table.
Source = str | list[str]
MINIMUM_DUCKDB = (1, 5, 0)
MEMORY_LIMIT = "4GB"
_EXTENSIONS = ("httpfs", "spatial")
# The temporary table that holds a copy's features before GDAL writes them.
_STAGED = "portolan_staged"
# The alias of a custom SELECT that a copy, a count, or a preview reads from.
_CUSTOM = "portolan_custom"
# The rows that a preview shows.
PREVIEW_ROWS = 100
# The statements that load the extensions in the DuckDB CLI.
SCRIPT_START = ("INSTALL spatial;", "LOAD spatial;", "INSTALL httpfs;", "LOAD httpfs;")
_lock = threading.Lock()
_connection: Any = None


class DuckDBMissingError(ImportError):
    """DuckDB is absent or older than ``MINIMUM_DUCKDB``."""


def _version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def duckdb_status() -> tuple[bool, str | None]:
    """Return whether DuckDB is usable, and its version when it is installed."""
    try:
        import duckdb
    except ImportError:
        return False, None
    version = str(duckdb.__version__)
    return _version(version) >= MINIMUM_DUCKDB, version


def is_flatpak() -> bool:
    """Return whether this Python runs inside a Flatpak sandbox."""
    return bool(os.environ.get("FLATPAK_ID")) or sys.prefix.startswith("/app")


def install_command(*, upgrade: bool) -> str:
    """Return the shell command that installs DuckDB for this QGIS."""
    minimum = ".".join(str(part) for part in MINIMUM_DUCKDB)
    flag = "--upgrade " if upgrade else ""
    if is_flatpak():
        return (
            "flatpak run --command=python3 org.qgis.qgis \\\n"  # i18n: skip - a command
            f"  -m pip install --user {flag}'duckdb>={minimum}'"  # i18n: skip - a command
        )
    return f"{sys.executable} -m pip install --user {flag}'duckdb>={minimum}'"  # i18n: skip - a command


def connection(extension_dir: str | None = None) -> Any:
    """Return the shared DuckDB connection with httpfs and spatial loaded.

    Raises:
        DuckDBMissingError: DuckDB is absent or too old.
    """
    global _connection  # noqa: PLW0603 - one connection per QGIS session
    ok, version = duckdb_status()
    if not ok:
        raise DuckDBMissingError(f"DuckDB {version}" if version else _("DuckDB is not installed"))
    import duckdb

    with _lock:
        if _connection is None:
            con = duckdb.connect()
            try:
                _load_extensions(con)
            except duckdb.IOException:
                if extension_dir is None:
                    raise
                con.execute("SET extension_directory = ?", [extension_dir])
                _load_extensions(con)
            con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
            if is_flatpak():
                # DuckDB's curl client crashes the process on any s3:// read in the Flatpak.
                con.execute("SET httpfs_client_implementation = 'httplib'")
            _connection = con
        return _connection


def _load_extensions(con: Any) -> None:
    for name in _EXTENSIONS:
        con.execute(f"INSTALL {name}")
        con.execute(f"LOAD {name}")


def close() -> None:
    """Close the shared connection. The plugin calls this on unload."""
    global _connection  # noqa: PLW0603 - see connection()
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


def interrupt_when(
    cancelled: Callable[[], bool] | None,
    cursor: Any,
    stop: threading.Event,
    progress: Progress | None = None,
) -> None:
    """Interrupt ``cursor`` once ``cancelled`` returns True, until ``stop`` is set.

    ``progress`` gets the share of the running query that DuckDB has done, from 0 to 1.
    """
    while not stop.wait(0.1):
        if cancelled is not None and cancelled():
            cursor.interrupt()
            return
        if progress is not None:
            done = cursor.query_progress()
            if done >= 0:
                progress(min(done, 100.0) / 100)


@contextlib.contextmanager
def cursor(
    con: Any, cancelled: Callable[[], bool] | None = None, progress: Progress | None = None
) -> Iterator[Any]:
    """Yield a cursor that a watcher thread interrupts when ``cancelled`` returns True.

    The running query then raises ``duckdb.InterruptException``. The watcher
    also reports the query's progress to ``progress``.
    """
    cur = con.cursor()
    stop = threading.Event()
    if progress is not None:
        cur.execute("SET enable_progress_bar = true")
        cur.execute("SET enable_progress_bar_print = false")
    if cancelled is not None or progress is not None:
        watcher = threading.Thread(
            target=interrupt_when, args=(cancelled, cur, stop, progress), daemon=True
        )
        watcher.start()
    try:
        yield cur
    finally:
        stop.set()
        cur.close()


def source_text(source: Source) -> str:
    """Return ``source`` as text for a layer property, one URL per line."""
    return source if isinstance(source, str) else "\n".join(source)


def source_from_text(text: str) -> Source:
    """Return the source that ``source_text`` wrote."""
    lines = [line for line in text.splitlines() if line]
    return lines if len(lines) > 1 else text.strip()


def bucket_secrets(source: Source) -> list[str]:
    """Return a ``CREATE SECRET`` for each S3 bucket of ``source`` whose name holds a dot.

    The virtual-hosted certificate covers one label, so such a bucket needs the path style.
    """
    urls = [source] if isinstance(source, str) else source
    buckets = {urlsplit(url).hostname for url in urls if url.startswith("s3://")}
    secrets = []
    for bucket in sorted(b for b in buckets if b and "." in b):
        name = "bucket_" + re.sub(r"\W", "_", bucket)
        region = bucket_region(bucket) or "us-east-1"
        secrets.append(
            f"CREATE SECRET IF NOT EXISTS {name} (TYPE s3, PROVIDER config, "
            f"REGION {literal(region)}, URL_STYLE 'path', SCOPE {literal(f's3://{bucket}')})"
        )
    return secrets


def use_bucket(con: Any, source: Source) -> None:
    """Give DuckDB the path style and region of each S3 bucket whose name holds a dot."""
    for secret in bucket_secrets(source):
        con.execute(secret)


def check_select(sql: str) -> str:
    """Return ``sql`` as one SELECT statement, without its final semicolon. Nothing runs.

    Raises:
        QueryError: The text does not parse, or is not exactly one SELECT.
        DuckDBMissingError: DuckDB is absent or too old.
    """
    ok, version = duckdb_status()
    if not ok:
        raise DuckDBMissingError(f"DuckDB {version}" if version else _("DuckDB is not installed"))
    import duckdb

    with contextlib.closing(duckdb.connect()) as parser:
        try:
            statements = parser.extract_statements(sql)
        except duckdb.Error as error:
            raise QueryError(str(error)) from error
    if len(statements) != 1:
        raise QueryError(
            ngettext(
                "Write one SELECT statement. The SQL holds {n} statement.",
                "Write one SELECT statement. The SQL holds {n} statements.",
                len(statements),
            ).format(n=len(statements))
        )
    if statements[0].type != duckdb.StatementType.SELECT:
        raise QueryError(_("The SQL must be a SELECT statement"))
    return without_terminator(str(statements[0].query)).strip()


def without_terminator(sql: str) -> str:
    """Return one SQL statement without its final semicolon and what follows it.

    A semicolon inside a string literal, a quoted name, or a comment stays.
    """
    index, closing = 0, None
    while index < len(sql):
        if closing is not None:
            if sql.startswith(closing, index):
                index, closing = index + len(closing), None
                continue
        elif sql.startswith("--", index):
            closing = "\n"
        elif sql.startswith("/*", index):
            closing = "*/"
        elif sql[index] in "'\"":
            closing = sql[index]
        elif sql[index] == ";":
            return sql[:index]
        index += 1
    return sql


def custom_table(sql: str) -> str:
    """Return the FROM item that reads the rows of the custom SELECT ``sql``.

    The line breaks keep a final ``--`` comment from hiding the parenthesis.
    """
    return f"(\n{sql}\n) AS {_CUSTOM}"


def read_error(source: Source, error: BaseException | str | None) -> str:
    """Return why DuckDB could not read ``source``, in words the user can act on."""
    text = str(error)
    urls = [source] if isinstance(source, str) else source
    buckets = sorted({urlsplit(url).hostname or "" for url in urls if url.startswith("s3://")})
    if "NoSuchBucket" in text and buckets:
        return _(
            "Amazon S3 has no bucket named {bucket}. The catalog gives no other S3 endpoint, "
            "so QGIS cannot read the files."
        ).format(bucket=buckets[0])
    return text


def list_files(con: Any, url: str, cancelled: Callable[[], bool] | None = None) -> list[str]:
    """Return the files that a bucket glob matches, in order."""
    use_bucket(con, url)
    with cursor(con, cancelled) as cur:
        return [
            str(row[0])
            for row in cur.execute("SELECT file FROM glob(?) ORDER BY file", [url]).fetchall()
        ]


def read_plan(con: Any, url: Source, cancelled: Callable[[], bool] | None = None) -> ReadPlan:
    """Read a file's schema and GeoParquet metadata and plan the query.

    ``url`` may be a bucket glob or a list of URLs, which DuckDB reads as one table.
    """
    use_bucket(con, url)
    with cursor(con, cancelled) as cur:
        schema = [
            (str(row[0]), str(row[1]))
            for row in cur.execute("DESCRIBE SELECT * FROM read_parquet(?)", [url]).fetchall()
        ]
        geo = cur.execute(
            "SELECT value FROM parquet_kv_metadata(?) WHERE key = 'geo' LIMIT 1", [url]
        ).fetchall()
    return plan_read(schema, geo[0][0] if geo else None)


@dataclass(frozen=True)
class Watch:
    """What a long query reports to: a cancel test, and a receiver of its progress."""

    cancelled: Callable[[], bool] | None = None
    progress: Progress | None = None


def estimate(
    con: Any,
    url: Source,
    plan: ReadPlan,
    rows: Rows = ALL_ROWS,
    watch: Watch | None = None,
) -> int | None:
    """Return about how many features a copy of the ``rows`` of ``url`` holds.

    With a clip the count is an upper bound, and custom SQL counts exactly.
    Returns None when the file has no covering column to count a box with.
    """
    if rows.query.sql is not None:
        counted: tuple[str, list[object]] | None = (
            f"SELECT count(*) FROM {custom_table(check_select(rows.query.sql))}",  # noqa: S608  # nosec B608 - the user's own SELECT, checked
            [],
        )
        use_bucket(con, url)
    else:
        counted = build_estimate(plan, rows.clip, selection(rows.query, plan))
        if counted is not None:
            counted = (counted[0], [url, *counted[1]])
    if counted is None:
        return None
    sql, params = counted
    watch = watch or Watch()
    with cursor(con, watch.cancelled, watch.progress) as cur:
        return int(cur.execute(sql, params).fetchone()[0])


@dataclass(frozen=True)
class Written:
    """What ``write_gpkg`` put in the GeoPackage."""

    features: int
    geometry_type: str
    # Features whose geometry type does not fit the layer. They keep their
    # attributes and have a NULL geometry.
    refused: int


@dataclass(frozen=True)
class Target:
    """The GeoPackage that ``write_gpkg`` writes, and what it keeps."""

    # The GeoPackage to write. It must not exist.
    path: str
    # Keep only the features in this clip, in the file's CRS.
    clip: Clip = WHOLE_FILE
    # The CRS to record in the GeoPackage.
    srs: str | None = None
    # The rows and columns to keep, or the SELECT that replaces them.
    query: Query = DEFAULT_QUERY


def _geometry_column(gpkg: sqlite3.Connection) -> tuple[str, str] | None:
    """Return the table and the geometry column of a GeoPackage's one layer."""
    row = gpkg.execute(
        "SELECT table_name, column_name FROM gpkg_geometry_columns"  # i18n: skip - SQL
    ).fetchone()
    return (str(row[0]), str(row[1])) if row is not None else None


def null_geometries(path: str) -> int:
    """Count the features with a NULL geometry in a GeoPackage.

    SQLite reads only the record headers to test for NULL, so the count
    stays fast on a large local file.
    """
    with contextlib.closing(sqlite3.connect(path)) as gpkg:
        found = _geometry_column(gpkg)
        if found is None:
            return 0
        table, column = (quote(part) for part in found)
        sql = f"SELECT count(*) FROM {table} WHERE {column} IS NULL"  # noqa: S608  # nosec B608 - names come from the GeoPackage and are quoted
        return int(gpkg.execute(sql).fetchone()[0])


def write_gpkg(
    con: Any,
    url: Source,
    plan: ReadPlan,
    target: Target,
    watch: Watch | None = None,
) -> Written:
    """Copy the features of ``url`` into a new GeoPackage at ``target.path``.

    A feature of another geometry type gets a NULL geometry and counts as
    refused. A table becomes an attribute table.
    """
    watch = watch or Watch()
    with cursor(con, watch.cancelled, watch.progress) as cur:
        # COPY to GDAL scans on one thread. A table first lets DuckDB read the row groups in parallel.
        if target.query.sql is None:
            layer_type = _stage(cur, url, plan, target)
        else:
            plan, layer_type = _stage_custom(cur, url, plan, target.query.sql)
        try:
            written = cur.execute(
                build_write(plan, f"SELECT * FROM {_STAGED}", target.path, layer_type, target.srs)  # noqa: S608  # nosec B608 - a fixed table name
            ).fetchone()
        finally:
            cur.execute(f"DROP TABLE IF EXISTS {_STAGED}")
    features = int(written[0]) if written else 0
    if not plan.has_geometry:
        drop_geometry(target.path)
        return Written(features, NO_GEOMETRY, 0)
    return Written(features, layer_type, null_geometries(target.path) if features else 0)


def _stage(cur: Any, url: Source, plan: ReadPlan, target: Target) -> str:
    """Put the rows of ``target`` in the staging table, and return the layer type."""
    chosen = selection(target.query, plan)
    layer_type = plan.geometry_type
    if layer_type == UNKNOWN:
        sql, params = build_first_type(plan, target.clip, chosen)
        layer_type = type_from_first(cur.execute(sql, [url, *params]).fetchone())
    query, params = build_select(plan, layer_type, target.clip, chosen)
    cur.execute(f"CREATE OR REPLACE TEMP TABLE {_STAGED} AS {query}", [url, *params])
    return layer_type


def _describe(cur: Any, sql: str, params: list[object]) -> list[tuple[str, str]]:
    """Return the names and DuckDB types of the columns of the rows of ``sql``."""
    rows = cur.execute(f"DESCRIBE SELECT * FROM (\n{sql}\n) AS portolan_described", params)  # noqa: S608  # nosec B608 - SQL the plugin built or checked
    return [(str(row[0]), str(row[1])) for row in rows.fetchall()]


def _stage_custom(cur: Any, url: Source, plan: ReadPlan, sql: str) -> tuple[ReadPlan, str]:
    """Put the rows of the custom SELECT ``sql`` in the staging table.

    Returns:
        The plan of those rows, and the layer type.
    """
    sql = check_select(sql)
    use_bucket(cur, url)
    staged = result_plan(plan, _describe(cur, sql, []))
    table = custom_table(sql)
    layer_type = staged.geometry_type
    if layer_type == UNKNOWN:
        first, params = build_first_type(staged, table=table)
        layer_type = type_from_first(cur.execute(first, params).fetchone())
    query, params = build_select(staged, layer_type, table=table)
    cur.execute(f"CREATE OR REPLACE TEMP TABLE {_STAGED} AS {query}", params)
    return staged, layer_type


def _rows_query(url: Source, plan: ReadPlan, rows: Rows) -> tuple[str, list[object]]:
    """Return the SELECT of the rows a copy keeps, and its parameters."""
    if rows.query.sql is not None:
        return check_select(rows.query.sql), []
    sql, params = build_select(plan, plan.geometry_type, rows.clip, selection(rows.query, plan))
    return sql, [url, *params]


def _shown(name: str, duckdb_type: str, plan: ReadPlan) -> str:
    """Return the SQL that shows one column in a preview, with a geometry as its type."""
    column = quote(name)
    upper = duckdb_type.upper()
    if upper.startswith("GEOMETRY"):
        return f"ST_GeometryType({column})::VARCHAR AS {column}"
    if upper == "BLOB" and name == plan.geometry:
        return f"ST_GeometryType(ST_GeomFromWKB({column}))::VARCHAR AS {column}"
    return column


@dataclass(frozen=True)
class Preview:
    """The first rows that a copy keeps, with the names of their columns."""

    columns: tuple[str, ...]
    rows: list[tuple[object, ...]]


def preview(
    con: Any,
    url: Source,
    plan: ReadPlan,
    rows: Rows = ALL_ROWS,
    watch: Watch | None = None,
) -> Preview:
    """Return the first ``PREVIEW_ROWS`` of the ``rows`` of ``url``.

    Each geometry shows as its type name.
    """
    sql, params = _rows_query(url, plan, rows)
    use_bucket(con, url)
    watch = watch or Watch()
    with cursor(con, watch.cancelled, watch.progress) as cur:
        schema = _describe(cur, sql, params)
        shown = ", ".join(_shown(name, duckdb_type, plan) for name, duckdb_type in schema)
        found = cur.execute(
            f"SELECT {shown} FROM (\n{sql}\n) AS portolan_preview LIMIT {PREVIEW_ROWS}",  # noqa: S608  # nosec B608 - SQL the plugin built or checked
            params,
        ).fetchall()
    return Preview(tuple(name for name, _type in schema), [tuple(row) for row in found])


def display_select(url: Source, plan: ReadPlan, rows: Rows = ALL_ROWS) -> str:
    """Return the SELECT of the rows a copy keeps, with every value in the text.

    Custom SQL comes back as the user wrote it.
    """
    if rows.query.sql is not None:
        return rows.query.sql.strip()
    sql, params = _rows_query(url, plan, rows)
    return pretty(inline(sql, params))


def copy_script(
    url: Source,
    plan: ReadPlan,
    path: str,
    rows: Rows = ALL_ROWS,
    srs: str | None = None,
) -> str:
    """Return a DuckDB CLI script that writes the copy of ``url`` to a GeoPackage at ``path``.

    The script puts every value into the SQL. Custom SQL goes in unchecked.
    """
    select = without_terminator(display_select(url, plan, rows)).strip()
    layer_type = UNKNOWN if rows.query.sql is not None else plan.geometry_type
    if not plan.has_geometry:
        layer_type = NO_GEOMETRY
    lines = [*SCRIPT_START, *(f"{secret};" for secret in bucket_secrets(url))]
    lines.append(build_write(plan, f"\n{select}\n", path, layer_type, srs) + ";")
    return "\n".join(lines) + "\n"


def drop_geometry(path: str) -> None:
    """Turn the one layer of a GeoPackage into an attribute table.

    DuckDB's GDAL writer always adds a geometry column. A table copy leaves
    it empty and without a spatial index, so the column drops cleanly. GDAL
    and QGIS then open the layer as a table with no geometry.
    """
    with contextlib.closing(sqlite3.connect(path)) as gpkg:
        found = _geometry_column(gpkg)
        if found is None:
            return
        table, column = found
        with gpkg:
            gpkg.execute("DELETE FROM gpkg_geometry_columns WHERE table_name = ?", [table])
            gpkg.execute(
                "UPDATE gpkg_contents SET data_type = 'attributes' WHERE table_name = ?", [table]
            )
            # DROP COLUMN needs SQLite 3.35, from March 2021.
            gpkg.execute(f"ALTER TABLE {quote(table)} DROP COLUMN {quote(column)}")
