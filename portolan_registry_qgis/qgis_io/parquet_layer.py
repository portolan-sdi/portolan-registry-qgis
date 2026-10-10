"""Load GeoParquet into QGIS through a GeoPackage that DuckDB writes.

``plan`` and ``prepare`` run off the main thread. ``plan`` reads the file's
schema and estimates the number of features, so the panel can ask before a
large load. ``prepare`` has DuckDB copy the features into a GeoPackage in
the plugin's scratch folder. No Python code touches a feature, so a load of
millions of features stays within the memory DuckDB uses. ``build`` runs on
the main thread and opens the GeoPackage through OGR.

Each QGIS session writes into its own folder. ``sweep`` deletes the files
that no layer in the project reads, and the plugin calls it when layers are
removed. ``remove_stale`` deletes the folders that an earlier session left.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsProviderRegistry,
    QgsRectangle,
    QgsVectorLayer,
)

from portolan_registry_qgis.core import parquet_query

if TYPE_CHECKING:
    from collections.abc import Callable

    from qgis.core import QgsCoordinateTransformContext

    from portolan_registry_qgis.core.geoparquet import ReadPlan

SOURCE_PROPERTY = "portolan/parquet_url"
# A session folder with no file newer than this belongs to a session that
# ended without cleaning up.
STALE_SECONDS = 7 * 24 * 3600
_SESSION_PREFIX = "session-"
# SQLite keeps these next to a GeoPackage while a connection is open.
_SIDECARS = ("", "-wal", "-shm", "-journal")
_lock = threading.Lock()
_session: Path | None = None
_pending: set[Path] = set()


@dataclass(frozen=True)
class Planned:
    """A GeoParquet file's read plan, ready to copy."""

    url: str
    plan: ReadPlan
    crs: QgsCoordinateReferenceSystem
    bbox: tuple[float, float, float, float] | None
    estimate: int | None


@dataclass(frozen=True)
class Prepared:
    """A GeoPackage that holds the features, ready to become a layer."""

    url: str
    path: Path
    written: parquet_query.Written


def crs_for(plan: ReadPlan) -> QgsCoordinateReferenceSystem:
    """Return the plan's CRS, or WGS84 when the file names none QGIS can read."""
    crs = QgsCoordinateReferenceSystem()
    if plan.crs and crs.createFromUserInput(plan.crs) and crs.isValid():
        return crs
    return QgsCoordinateReferenceSystem("EPSG:4326")


def _profile_folder(name: str) -> Path:
    folder = Path(QgsApplication.qgisSettingsDirPath()) / "portolan_registry" / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def extension_directory() -> str:
    """Return a writable folder for DuckDB extensions in the QGIS profile."""
    return str(_profile_folder("duckdb"))


def scratch_folder() -> Path:
    """Return the folder that holds every session's GeoPackages.

    It is in the QGIS profile, not the system temporary folder, which is
    memory on many Linux systems.
    """
    return _profile_folder("layers")


def file_bbox(
    extent: QgsRectangle | None,
    extent_crs: QgsCoordinateReferenceSystem | None,
    file_crs: QgsCoordinateReferenceSystem,
    context: QgsCoordinateTransformContext,
) -> tuple[float, float, float, float] | None:
    """Transform the map extent into the file's CRS."""
    if extent is None or extent_crs is None:
        return None
    rect = QgsCoordinateTransform(extent_crs, file_crs, context).transformBoundingBox(extent)
    return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())


def plan(
    url: str,
    context: QgsCoordinateTransformContext,
    extent: QgsRectangle | None = None,
    extent_crs: QgsCoordinateReferenceSystem | None = None,
    extension_dir: str | None = None,
) -> Planned:
    """Read a GeoParquet file's schema and estimate its features. Safe off the main thread.

    Args:
        url: The file's URL or local path.
        context: The project's transform context, for the extent.
        extent: Keep only features that intersect this rectangle.
        extent_crs: The CRS of ``extent``.
        extension_dir: Where DuckDB installs extensions if its default
            folder is read-only.

    Raises:
        parquet_query.DuckDBMissingError: DuckDB is absent or too old.
        GeoParquetError: The file has no geometry column.
    """
    con = parquet_query.connection(extension_dir)
    read = parquet_query.read_plan(con, url)
    crs = crs_for(read)
    bbox = file_bbox(extent, extent_crs, crs, context)
    return Planned(url, read, crs, bbox, parquet_query.estimate(con, url, read, bbox))


def prepare(
    planned: Planned,
    extension_dir: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Prepared:
    """Copy the planned features into a new GeoPackage. Safe off the main thread.

    Args:
        planned: The result of ``plan``.
        extension_dir: As for ``plan``.
        cancelled: Polled during the copy. When it returns True, the copy
            stops and raises ``duckdb.InterruptException``.
    """
    con = parquet_query.connection(extension_dir)
    path = _new_path()
    srs = planned.crs.authid() or planned.plan.crs or None
    try:
        written = parquet_query.write_gpkg(
            con, planned.url, planned.plan, str(path), planned.bbox, srs, cancelled
        )
    except BaseException:
        discard_path(path)
        raise
    return Prepared(planned.url, path, written)


def build(prepared: Prepared, name: str) -> tuple[QgsVectorLayer, int]:
    """Open the GeoPackage as a layer. Call on the main thread.

    Returns:
        The layer, and how many features the copy left out because their
        geometry type did not fit the layer.

    Raises:
        OSError: QGIS cannot open the GeoPackage.
    """
    layer = QgsVectorLayer(str(prepared.path), name, "ogr")
    if not layer.isValid():
        discard_path(prepared.path)
        raise OSError(f"QGIS could not open {prepared.path}: {layer.error().summary()}")
    with _lock:
        _pending.discard(prepared.path)
    layer.setCustomProperty(SOURCE_PROPERTY, prepared.url)
    return layer, prepared.written.refused


def _new_path() -> Path:
    global _session  # noqa: PLW0603 - one scratch folder per QGIS session
    with _lock:
        if _session is None or not _session.is_dir():
            _session = Path(tempfile.mkdtemp(prefix=_SESSION_PREFIX, dir=scratch_folder()))
        path = _session / f"{uuid.uuid4().hex}.gpkg"
        _pending.add(path)
        return path


def _remove(path: Path) -> None:
    """Delete a GeoPackage and its SQLite sidecars.

    Windows refuses to delete a file that is still open. The next sweep, or
    ``remove_stale`` in a later session, deletes it then.
    """
    for suffix in _SIDECARS:
        with contextlib.suppress(OSError):
            Path(f"{path}{suffix}").unlink(missing_ok=True)


def discard_path(path: Path) -> None:
    """Delete a GeoPackage that no layer reads."""
    with _lock:
        _pending.discard(path)
    _remove(path)


def _in_use(project: QgsProject) -> set[Path]:
    registry = QgsProviderRegistry.instance()
    paths = set()
    for layer in project.mapLayers().values():
        if layer.providerType() == "ogr":
            decoded = registry.decodeUri("ogr", layer.source()).get("path")
            if decoded:
                paths.add(Path(decoded))
    return paths


def sweep(project: QgsProject | None = None) -> None:
    """Delete this session's GeoPackages that no layer in ``project`` reads.

    A layer the user duplicated reads the same file, so the file stays until
    the last layer that reads it is gone.
    """
    with _lock:
        session = _session
        keep = set(_pending)
    if session is None or not session.is_dir():
        return
    keep |= _in_use(project or QgsProject.instance())
    for path in session.glob("*.gpkg"):
        if path not in keep:
            _remove(path)


def close_session(project: QgsProject | None = None) -> None:
    """Sweep, then delete this session's folder if it is empty. Call on unload."""
    global _session  # noqa: PLW0603 - see _new_path
    sweep(project)
    with _lock:
        session, _session = _session, None
        _pending.clear()
    if session is not None:
        with contextlib.suppress(OSError):
            session.rmdir()


def remove_stale(now: float | None = None) -> None:
    """Delete the session folders of earlier sessions that ended without cleaning up.

    Another QGIS that runs at the same time keeps its folder, because its
    files are newer than ``STALE_SECONDS``.
    """
    cutoff = (now if now is not None else time.time()) - STALE_SECONDS
    for folder in scratch_folder().glob(f"{_SESSION_PREFIX}*"):
        if folder == _session or not folder.is_dir():
            continue
        times = [entry.stat().st_mtime for entry in folder.iterdir()]
        if max(times, default=folder.stat().st_mtime) < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
