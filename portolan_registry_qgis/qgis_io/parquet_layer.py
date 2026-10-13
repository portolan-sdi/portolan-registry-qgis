"""Load GeoParquet into QGIS through a GeoPackage that DuckDB writes.

``plan`` and ``prepare`` run off the main thread. ``plan`` reads the file's
schema and estimates the number of features, so the panel can ask before a
large load. ``prepare`` has DuckDB copy the features into a GeoPackage in
the plugin's scratch folder. No Python code touches a feature, so a load of
millions of features stays within the memory DuckDB uses. ``build`` runs on
the main thread and opens the GeoPackage through OGR. A file with no
geometry column becomes a table with no geometry.

A copy can keep only the features in a box or in a polygon. The layer keeps
both in the file's CRS, in ``BBOX_PROPERTY`` and ``AREA_PROPERTY``.

Each QGIS session writes into its own folder. ``sweep`` deletes the files
that no layer in the project reads, and the plugin calls it when layers are
removed. ``remove_stale`` deletes the folders that an earlier session left.
The mosaics of ``mosaic`` write their VRT files into the same folders, so
the same functions clean them.

A saved project keeps the path of a file that a later session no longer
has. ``restore_path`` gives QGIS an empty placeholder in its place, so the
layer opens. ``restore`` then copies the features again from the URL, the
box, and the area that the layer keeps, and ``reopen`` points the layer at
the copy.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFields,
    QgsGeometry,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.geoparquet import LAYER, WHOLE_FILE, Clip
from portolan_registry_qgis.core.i18n import _

if TYPE_CHECKING:
    from collections.abc import Callable

    from qgis.core import QgsCoordinateTransformContext

    from portolan_registry_qgis.core.geoparquet import ReadPlan
    from portolan_registry_qgis.core.stac import Bbox

SOURCE_PROPERTY = "portolan/parquet_url"
# The box of the copy in the file's CRS, as "west,south,east,north", or
# empty for the whole file.
BBOX_PROPERTY = "portolan/parquet_bbox"
# The polygon of the copy in the file's CRS, as WKT, or empty when the copy
# has no polygon.
AREA_PROPERTY = "portolan/parquet_area"
# A session folder with no file newer than this belongs to a session that
# ended without cleaning up.
STALE_SECONDS = 7 * 24 * 3600
_SESSION_PREFIX = "session-"
_PLACEHOLDER = "placeholder.gpkg"  # i18n: skip - a file name
_PLACEHOLDER_VRT = "placeholder.vrt"  # i18n: skip - a file name
# A raster with one empty pixel. It stands in for a mosaic a saved project lost.
_EMPTY_VRT = (
    '<VRTDataset rasterXSize="1" rasterYSize="1">'  # i18n: skip - VRT XML
    "<SRS>EPSG:4326</SRS><GeoTransform>0, 1, 0, 0, 0, -1</GeoTransform>"
    '<VRTRasterBand dataType="Byte" band="1"><NoDataValue>0</NoDataValue></VRTRasterBand>'
    "</VRTDataset>\n"
)
# The file types the session folders hold: GeoParquet copies and mosaics.
_SUFFIXES = (".gpkg", ".vrt")
# A copy's or a mosaic's path, and the "|layername=..." part of an OGR
# source after it.
_SCRATCH_FILE = re.compile(
    r"portolan_registry/layers/(session-[^/]+)/([0-9a-f]{32}\.(?:gpkg|vrt))(\|.*)?$"
)
# SQLite keeps the first four next to a GeoPackage while a connection is
# open. GDAL writes raster statistics to the last.
_SIDECARS = ("", "-wal", "-shm", "-journal", ".aux.xml")  # i18n: skip - file suffixes
_lock = threading.Lock()
_session: Path | None = None
_pending: set[Path] = set()


@dataclass(frozen=True)
class Planned:
    """A GeoParquet file's read plan, ready to copy."""

    url: str
    plan: ReadPlan
    crs: QgsCoordinateReferenceSystem
    # The part of the file the copy keeps, in the file's CRS.
    clip: Clip
    estimate: int | None


@dataclass(frozen=True)
class Prepared:
    """A GeoPackage that holds the features, ready to become a layer."""

    url: str
    path: Path
    written: parquet_query.Written
    clip: Clip = WHOLE_FILE


def crs_for(plan: ReadPlan) -> QgsCoordinateReferenceSystem:
    """Return the plan's CRS, or WGS84 when the file names none QGIS can read."""
    crs = QgsCoordinateReferenceSystem()
    if plan.crs and crs.createFromUserInput(plan.crs) and crs.isValid():
        return crs
    return QgsCoordinateReferenceSystem("EPSG:4326")


def _profile_folder(name: str, *, create: bool = True) -> Path:
    folder = Path(QgsApplication.qgisSettingsDirPath()) / "portolan_registry" / name
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def extension_directory() -> str:
    """Return a writable folder for DuckDB extensions in the QGIS profile."""
    return str(_profile_folder("duckdb"))


def scratch_folder(*, create: bool = True) -> Path:
    """Return the folder that holds every session's GeoPackages.

    It is in the QGIS profile, not the system temporary folder, which is
    memory on many Linux systems.
    """
    return _profile_folder("layers", create=create)


def file_bbox(
    extent: QgsRectangle | None,
    extent_crs: QgsCoordinateReferenceSystem | None,
    file_crs: QgsCoordinateReferenceSystem,
    context: QgsCoordinateTransformContext,
) -> Bbox | None:
    """Transform the map extent into the file's CRS."""
    if extent is None or extent_crs is None:
        return None
    rect = QgsCoordinateTransform(extent_crs, file_crs, context).transformBoundingBox(extent)
    return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())


def file_area(
    area: str,
    file_crs: QgsCoordinateReferenceSystem,
    context: QgsCoordinateTransformContext,
) -> Clip:
    """Transform a polygon in EPSG:4326 into the file's CRS.

    Returns:
        The polygon as WKT in the file's CRS, with its envelope as the box.

    Raises:
        ValueError: ``area`` is not a Polygon or MultiPolygon WKT, or the
            transform fails.
    """
    geometry = QgsGeometry.fromWkt(area)
    if geometry.isNull() or geometry.type() != Qgis.GeometryType.Polygon:
        raise ValueError(_("Not a Polygon or MultiPolygon: {wkt}").format(wkt=area[:80]))
    transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), file_crs, context)
    if geometry.transform(transform) != Qgis.GeometryOperationResult.Success:
        crs = file_crs.authid()
        if crs:
            raise ValueError(_("Cannot transform the area into {crs}").format(crs=crs))
        raise ValueError(_("Cannot transform the area into the file CRS"))
    rect = geometry.boundingBox()
    envelope = (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
    return Clip(envelope, geometry.asWkt())


def plan(
    url: str,
    context: QgsCoordinateTransformContext,
    extent: QgsRectangle | None = None,
    extent_crs: QgsCoordinateReferenceSystem | None = None,
    extension_dir: str | None = None,
    cancelled: Callable[[], bool] | None = None,
    area: str | None = None,
) -> Planned:
    """Read a GeoParquet file's schema and estimate its features. Safe off the main thread.

    Args:
        url: The file's URL or local path.
        context: The project's transform context, for the extent and the area.
        extent: Keep only features that intersect this rectangle.
        extent_crs: The CRS of ``extent``.
        extension_dir: Where DuckDB installs extensions if its default
            folder is read-only.
        cancelled: Polled while the queries run. When it returns True, the
            running query stops and raises ``duckdb.InterruptException``.
        area: Keep only features that intersect this Polygon or
            MultiPolygon WKT in EPSG:4326. It replaces ``extent``.

    Raises:
        parquet_query.DuckDBMissingError: DuckDB is absent or too old.
        GeoParquetError: The geo metadata names a column the file does not have.
        ValueError: ``area`` is not a polygon the file's CRS can hold.
    """
    con = parquet_query.connection(extension_dir)
    read = parquet_query.read_plan(con, url, cancelled)
    crs = crs_for(read)
    if area is None:
        clip = Clip(file_bbox(extent, extent_crs, crs, context))
    else:
        clip = file_area(area, crs, context)
    count = parquet_query.estimate(con, url, read, clip, cancelled)
    return Planned(url, read, crs, clip, count)


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
    path = new_path(".gpkg")
    srs = planned.crs.authid() or planned.plan.crs or None
    try:
        target = parquet_query.Target(str(path), planned.clip, srs)
        written = parquet_query.write_gpkg(con, planned.url, planned.plan, target, cancelled)
    except BaseException:
        discard_path(path)
        raise
    return Prepared(planned.url, path, written, planned.clip)


def build(prepared: Prepared, name: str) -> tuple[QgsVectorLayer, int]:
    """Open the GeoPackage as a layer. Call on the main thread.

    A copy of a file with no geometry column holds an attribute table. It
    opens as a layer with no geometry.

    Returns:
        The layer, and how many features the copy left out because their
        geometry type did not fit the layer.

    Raises:
        OSError: QGIS cannot open the GeoPackage.
    """
    # The layer name selects the table. A GeoPackage that holds only an
    # attribute table has no geometry layer for OGR to pick first.
    layer = QgsVectorLayer(f"{prepared.path}|layername={LAYER}", name, "ogr")
    if not layer.isValid():
        discard_path(prepared.path)
        raise OSError(
            _("QGIS could not open {path}: {error}").format(
                path=prepared.path, error=layer.error().summary()
            )
        )
    release_path(prepared.path)
    layer.setCustomProperty(SOURCE_PROPERTY, prepared.url)
    bbox, area = prepared.clip.to_text()
    layer.setCustomProperty(BBOX_PROPERTY, bbox)
    layer.setCustomProperty(AREA_PROPERTY, area)
    return layer, prepared.written.refused


def placeholder() -> Path:
    """Return an empty GeoPackage that stands in for a file a saved project lost.

    It holds one empty layer with the name every copy uses, so the layer
    source a project saved opens without an error.
    """
    path = scratch_folder() / _PLACEHOLDER
    if not path.exists():
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = LAYER
        writer = QgsVectorFileWriter.create(
            str(path),
            QgsFields(),
            Qgis.WkbType.Unknown,
            QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance().transformContext(),
            options,
        )
        del writer
    return path


def placeholder_vrt() -> Path:
    """Return a one-pixel empty raster that stands in for a mosaic a saved project lost."""
    path = scratch_folder() / _PLACEHOLDER_VRT
    if not path.exists():
        path.write_text(_EMPTY_VRT, encoding="utf-8")
    return path


def is_placeholder(path: Path) -> bool:
    """Return whether ``path`` is one of the placeholders of ``restore_path``."""
    folder = scratch_folder(create=False)
    return path in {folder / _PLACEHOLDER, folder / _PLACEHOLDER_VRT}


def is_lost_copy(path: Path) -> bool:
    """Return whether ``path`` is a placeholder, or a scratch file that is gone."""
    if is_placeholder(path):
        return True
    return _SCRATCH_FILE.search(path.as_posix()) is not None and not path.exists()


def restore_path(path: str) -> str:
    """Map the path of a copy or a mosaic in a saved project to a file that exists.

    QGIS calls this for every path it reads from a project. The path stays
    when the file exists. Otherwise a placeholder takes its place, and
    ``needs_restore`` or ``mosaic.needs_rebuild`` then reports the layer.
    """
    match = _SCRATCH_FILE.search(path.replace("\\", "/"))
    if match is None:
        return path
    current = scratch_folder(create=False) / match.group(1) / match.group(2)
    suffix = match.group(3) or ""
    if current.exists():
        return f"{current}{suffix}"
    try:
        stand_in = placeholder_vrt() if current.suffix == ".vrt" else placeholder()
    except OSError:
        return path
    return f"{stand_in}{suffix}"


def _source_path(layer: QgsVectorLayer) -> Path | None:
    decoded = QgsProviderRegistry.instance().decodeUri("ogr", layer.source()).get("path")
    return Path(decoded) if decoded else None


def needs_restore(layer: object) -> bool:
    """Return whether ``layer`` is a copy whose file is gone.

    With ``restore_path`` in place, such a layer reads the placeholder. On
    QGIS 3.34, the plugin cannot register ``restore_path``, so the layer
    keeps the path of the missing copy and opens as unavailable.
    """
    if not (
        isinstance(layer, QgsVectorLayer)
        and bool(layer.customProperty(SOURCE_PROPERTY))
        and layer.providerType() == "ogr"
    ):
        return False
    path = _source_path(layer)
    return path is not None and is_lost_copy(path)


def restore(
    layer_url: str,
    bbox_text: str,
    extension_dir: str | None = None,
    cancelled: Callable[[], bool] | None = None,
    area_text: str = "",
) -> Prepared:
    """Copy a saved layer's features again. Safe off the main thread.

    Args:
        layer_url: The value of ``SOURCE_PROPERTY``.
        bbox_text: The value of ``BBOX_PROPERTY``.
        extension_dir: As for ``plan``.
        cancelled: As for ``prepare``.
        area_text: The value of ``AREA_PROPERTY``. It is already in the
            file's CRS.
    """
    con = parquet_query.connection(extension_dir)
    read = parquet_query.read_plan(con, layer_url, cancelled)
    clip = Clip.from_text(bbox_text, area_text)
    planned = Planned(layer_url, read, crs_for(read), clip, None)
    return prepare(planned, extension_dir, cancelled)


def saved_copy(layer: QgsVectorLayer) -> tuple[str, str, str]:
    """Return what ``restore`` needs from a saved layer: its URL, box, and area."""
    return (
        str(layer.customProperty(SOURCE_PROPERTY) or ""),
        str(layer.customProperty(BBOX_PROPERTY) or ""),
        str(layer.customProperty(AREA_PROPERTY) or ""),
    )


def reopen(layer: QgsVectorLayer, prepared: Prepared) -> None:
    """Point a restored layer at its new copy. Call on the main thread.

    Raises:
        OSError: QGIS cannot open the copy.
    """
    layer.setDataSource(f"{prepared.path}|layername={LAYER}", layer.name(), "ogr")
    if not layer.isValid():
        discard_path(prepared.path)
        raise OSError(_("QGIS could not open {path}").format(path=prepared.path))
    release_path(prepared.path)
    layer.triggerRepaint()


def _own_prefix() -> str:
    # The process id tells this QGIS's folders apart from another QGIS's,
    # and keeps them when the plugin reloads and forgets ``_session``.
    return f"{_SESSION_PREFIX}{os.getpid()}-"


def _own_sessions() -> list[Path]:
    folder = scratch_folder(create=False)
    return [path for path in folder.glob(f"{_own_prefix()}*") if path.is_dir()]


def new_path(suffix: str) -> Path:
    """Return a new file path in this session's scratch folder.

    The sweep keeps the file until ``release_path`` or ``discard_path``
    runs, because no layer reads it yet.

    Args:
        suffix: ``.gpkg`` for a GeoParquet copy, ``.vrt`` for a mosaic.
    """
    global _session  # noqa: PLW0603 - one scratch folder per QGIS session
    if suffix not in _SUFFIXES:
        # A programming error. The callers pass a suffix from _SUFFIXES.
        raise ValueError(f"The scratch folder holds no {suffix} files")  # i18n: skip
    with _lock:
        if _session is None or not _session.is_dir():
            _session = Path(tempfile.mkdtemp(prefix=_own_prefix(), dir=scratch_folder()))
        path = _session / f"{uuid.uuid4().hex}{suffix}"
        _pending.add(path)
        return path


def release_path(path: Path) -> None:
    """Let the sweep delete ``path`` once no layer reads it. Call after a layer opens it."""
    with _lock:
        _pending.discard(path)


def _remove(path: Path) -> None:
    """Delete a scratch file and its sidecars.

    Windows refuses to delete a file that is still open. The next sweep, or
    ``remove_stale`` in a later session, deletes it then.
    """
    for suffix in _SIDECARS:
        with contextlib.suppress(OSError):
            Path(f"{path}{suffix}").unlink(missing_ok=True)


def discard_path(path: Path) -> None:
    """Delete a scratch file that no layer reads."""
    with _lock:
        _pending.discard(path)
    _remove(path)


def _in_use(project: QgsProject) -> set[Path]:
    paths = set()
    for layer in project.mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and layer.providerType() == "ogr":
            path = _source_path(layer)
        elif isinstance(layer, QgsRasterLayer) and layer.providerType() == "gdal":
            decoded = QgsProviderRegistry.instance().decodeUri("gdal", layer.source()).get("path")
            path = Path(decoded) if decoded else None
        else:
            continue
        if path is not None:
            paths.add(path)
    return paths


def sweep(project: QgsProject | None = None) -> None:
    """Delete this QGIS's GeoPackages and mosaics that no layer in ``project`` reads.

    A layer the user duplicated reads the same file, so the file stays until
    the last layer that reads it is gone. The sweep also covers the folders
    of earlier plugin loads in this QGIS.
    """
    with _lock:
        keep = set(_pending)
    keep |= _in_use(project or QgsProject.instance())
    for session in _own_sessions():
        for suffix in _SUFFIXES:
            for path in session.glob(f"*{suffix}"):
                if path not in keep:
                    _remove(path)


def close_session(project: QgsProject | None = None) -> None:
    """Sweep, then delete this QGIS's empty session folders. Call on unload."""
    global _session  # noqa: PLW0603 - see new_path
    sweep(project)
    with _lock:
        _session = None
        _pending.clear()
    for session in _own_sessions():
        with contextlib.suppress(OSError):
            session.rmdir()


def _newest(folder: Path) -> float:
    times = []
    for entry in folder.iterdir():
        with contextlib.suppress(OSError):
            times.append(entry.stat().st_mtime)
    return max(times, default=folder.stat().st_mtime)


def remove_stale(now: float | None = None) -> None:
    """Delete the session folders of earlier sessions that ended without cleaning up.

    Another QGIS that runs at the same time keeps its folder, because its
    files are newer than ``STALE_SECONDS``. A file that another QGIS deletes
    during the scan, or a folder the plugin cannot read, is skipped.
    """
    cutoff = (now if now is not None else time.time()) - STALE_SECONDS
    folder = scratch_folder(create=False)
    if not folder.is_dir():
        return
    for session in folder.glob(f"{_SESSION_PREFIX}*"):
        if session.name.startswith(_own_prefix()):
            continue
        try:
            stale = session.is_dir() and _newest(session) < cutoff
        except OSError:
            continue
        if stale:
            shutil.rmtree(session, ignore_errors=True)
