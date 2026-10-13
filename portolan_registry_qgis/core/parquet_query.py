"""Copy GeoParquet over HTTP into a GeoPackage with DuckDB.

DuckDB writes the GeoPackage through the GDAL that its spatial extension
bundles, so no Python code touches a feature. QGIS then opens the file
through OGR. DuckDB is an external dependency. QGIS does not ship it, so every function
imports it lazily and the plugin loads without it. ``duckdb_status`` reports
whether a usable version is present, and the GUI tells the user how to install
it when it is not.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from portolan_registry_qgis.core.geoparquet import (
    NO_GEOMETRY,
    WHOLE_FILE,
    Clip,
    ReadPlan,
    build_copy,
    build_estimate,
    build_first_type,
    plan_read,
    quote,
    type_from_first,
)
from portolan_registry_qgis.core.i18n import _

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

MINIMUM_DUCKDB = (1, 5, 0)
MEMORY_LIMIT = "4GB"
_EXTENSIONS = ("httpfs", "spatial")
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

    Args:
        extension_dir: Where DuckDB installs extensions when its default
            directory is not writable, as in some sandboxes.

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


def interrupt_when(cancelled: Callable[[], bool], cursor: Any, stop: threading.Event) -> None:
    """Interrupt ``cursor`` once ``cancelled`` returns True, until ``stop`` is set."""
    while not stop.wait(0.1):
        if cancelled():
            cursor.interrupt()
            return


@contextlib.contextmanager
def cursor(con: Any, cancelled: Callable[[], bool] | None = None) -> Iterator[Any]:
    """Yield a cursor that a watcher thread interrupts when ``cancelled`` returns True.

    The running query then raises ``duckdb.InterruptException``.
    """
    cur = con.cursor()
    stop = threading.Event()
    if cancelled is not None:
        threading.Thread(target=interrupt_when, args=(cancelled, cur, stop), daemon=True).start()
    try:
        yield cur
    finally:
        stop.set()
        cur.close()


def read_plan(con: Any, url: str, cancelled: Callable[[], bool] | None = None) -> ReadPlan:
    """Read a file's schema and GeoParquet metadata and plan the query."""
    with cursor(con, cancelled) as cur:
        schema = [
            (str(row[0]), str(row[1]))
            for row in cur.execute("DESCRIBE SELECT * FROM read_parquet(?)", [url]).fetchall()
        ]
        geo = cur.execute(
            "SELECT value FROM parquet_kv_metadata(?) WHERE key = 'geo' LIMIT 1", [url]
        ).fetchall()
    return plan_read(schema, geo[0][0] if geo else None)


def estimate(
    con: Any,
    url: str,
    plan: ReadPlan,
    clip: Clip = WHOLE_FILE,
    cancelled: Callable[[], bool] | None = None,
) -> int | None:
    """Return about how many features a copy of ``url`` in ``clip`` holds.

    The count is exact without a box. With a box it counts the rows whose
    bbox overlaps it, an upper bound. With an area it still counts by the
    area's envelope, so it stays an upper bound. A table has no geometry,
    so its count is exact.

    Args:
        con: The DuckDB connection.
        url: The Parquet file.
        plan: The file's read plan.
        clip: The clip of the copy, in the file's CRS.
        cancelled: Polled while the query runs.

    Returns:
        The estimate, or None when the file has no covering column to count
        a box with cheaply.
    """
    query = build_estimate(plan, clip)
    if query is None:
        return None
    sql, params = query
    with cursor(con, cancelled) as cur:
        return int(cur.execute(sql, [url, *params]).fetchone()[0])


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
    url: str,
    plan: ReadPlan,
    target: Target,
    cancelled: Callable[[], bool] | None = None,
) -> Written:
    """Copy the features of ``url`` into a new GeoPackage at ``target.path``.

    A layer holds one geometry type. When the file declares none, the first
    geometry picks it. A feature of another type keeps its attributes, gets
    a NULL geometry, and counts as refused.

    Args:
        con: The DuckDB connection.
        url: The GeoParquet file.
        plan: The file's read plan.
        target: The GeoPackage to write, its clip, and its CRS.
        cancelled: Polled while the queries run. When it returns True, the
            running query stops and raises ``duckdb.InterruptException``.

    A table, a file with no geometry column, becomes an attribute table with
    every row. The clip and the CRS do not apply to it, and its
    ``Written.geometry_type`` is ``NO_GEOMETRY``.
    """
    with cursor(con, cancelled) as cur:
        layer_type = plan.geometry_type
        if layer_type == "Unknown":
            sql, params = build_first_type(plan, target.clip)
            layer_type = type_from_first(cur.execute(sql, [url, *params]).fetchone())
        sql, params = build_copy(plan, target.path, layer_type, target.clip, target.srs)
        written = cur.execute(sql, [url, *params]).fetchone()
    features = int(written[0]) if written else 0
    if not plan.has_geometry:
        drop_geometry(target.path)
        return Written(features, NO_GEOMETRY, 0)
    return Written(features, layer_type, null_geometries(target.path) if features else 0)


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
