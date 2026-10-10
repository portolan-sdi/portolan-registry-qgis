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
    ReadPlan,
    build_copy,
    build_estimate,
    build_first_type,
    plan_read,
    type_from_first,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

Bbox = tuple[float, float, float, float]

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


def install_command(upgrade: bool) -> str:
    """Return the shell command that installs DuckDB for this QGIS."""
    minimum = ".".join(str(part) for part in MINIMUM_DUCKDB)
    flag = "--upgrade " if upgrade else ""
    if is_flatpak():
        return (
            "flatpak run --command=python3 org.qgis.qgis \\\n"
            f"  -m pip install --user {flag}'duckdb>={minimum}'"
        )
    return f"{sys.executable} -m pip install --user {flag}'duckdb>={minimum}'"


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
        raise DuckDBMissingError(f"DuckDB {version or 'is not installed'}")
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


def _interrupt_when(cancelled: Callable[[], bool], cursor: Any, stop: threading.Event) -> None:
    while not stop.wait(0.1):
        if cancelled():
            cursor.interrupt()
            return


@contextlib.contextmanager
def _cursor(con: Any, cancelled: Callable[[], bool] | None = None) -> Iterator[Any]:
    """Yield a cursor that a watcher thread interrupts when ``cancelled`` returns True.

    The running query then raises ``duckdb.InterruptException``.
    """
    cursor = con.cursor()
    stop = threading.Event()
    if cancelled is not None:
        threading.Thread(
            target=_interrupt_when, args=(cancelled, cursor, stop), daemon=True
        ).start()
    try:
        yield cursor
    finally:
        stop.set()
        cursor.close()


def read_plan(con: Any, url: str, cancelled: Callable[[], bool] | None = None) -> ReadPlan:
    """Read a file's schema and GeoParquet metadata and plan the query."""
    with _cursor(con, cancelled) as cursor:
        schema = [
            (str(row[0]), str(row[1]))
            for row in cursor.execute("DESCRIBE SELECT * FROM read_parquet(?)", [url]).fetchall()
        ]
        geo = cursor.execute(
            "SELECT value FROM parquet_kv_metadata(?) WHERE key = 'geo' LIMIT 1", [url]
        ).fetchall()
    return plan_read(schema, geo[0][0] if geo else None)


def estimate(
    con: Any,
    url: str,
    plan: ReadPlan,
    bbox: Bbox | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> int | None:
    """Return about how many features a copy of ``url`` in ``bbox`` holds.

    The count is exact without a box. With a box it counts the rows whose
    bbox overlaps it, an upper bound.

    Returns:
        The estimate, or None when the file has no covering column to count
        a box with cheaply.
    """
    query = build_estimate(plan, bbox)
    if query is None:
        return None
    sql, params = query
    with _cursor(con, cancelled) as cursor:
        return int(cursor.execute(sql, [url, *params]).fetchone()[0])


@dataclass(frozen=True)
class Written:
    """What ``write_gpkg`` put in the GeoPackage."""

    features: int
    geometry_type: str
    # Features whose geometry type does not fit the layer. They keep their
    # attributes and have a NULL geometry.
    refused: int


def _null_geometries(path: str) -> int:
    """Count the features with a NULL geometry in a GeoPackage.

    SQLite reads only the record headers to test for NULL, so the count
    stays fast on a large local file.
    """
    with contextlib.closing(sqlite3.connect(path)) as gpkg:
        row = gpkg.execute("SELECT table_name, column_name FROM gpkg_geometry_columns").fetchone()
        if row is None:
            return 0
        table, column = (str(part).replace('"', '""') for part in row)
        sql = f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NULL'  # noqa: S608 - names come from the GeoPackage and are quoted
        return int(gpkg.execute(sql).fetchone()[0])


def write_gpkg(
    con: Any,
    url: str,
    plan: ReadPlan,
    path: str,
    bbox: Bbox | None = None,
    srs: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Written:
    """Copy the features of ``url`` into a new GeoPackage at ``path``.

    A layer holds one geometry type. When the file declares none, the first
    geometry picks it. A feature of another type keeps its attributes, gets
    a NULL geometry, and counts as refused.

    Args:
        con: The DuckDB connection.
        url: The GeoParquet file.
        plan: The file's read plan.
        path: The GeoPackage to write. It must not exist.
        bbox: Keep only features that intersect this box, in the file's CRS.
        srs: The CRS to record in the GeoPackage.
        cancelled: Polled while the queries run. When it returns True, the
            running query stops and raises ``duckdb.InterruptException``.
    """
    with _cursor(con, cancelled) as cursor:
        layer_type = plan.geometry_type
        if layer_type == "Unknown":
            sql, params = build_first_type(plan, bbox)
            layer_type = type_from_first(cursor.execute(sql, [url, *params]).fetchone())
        sql, params = build_copy(plan, path, layer_type, bbox, srs)
        written = cursor.execute(sql, [url, *params]).fetchone()
    features = int(written[0]) if written else 0
    return Written(features, layer_type, _null_geometries(path) if features else 0)
