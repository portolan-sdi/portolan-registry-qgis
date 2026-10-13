"""Add many COG files as one raster layer.

A collection of COG items becomes a GDAL VRT over the ``/vsicurl/`` paths
of the items that intersect the scope. QGIS reads only the tiles on screen.

``item_hrefs_in`` and ``write_vrt`` send network requests, so they run off
the main thread. ``start_vrt`` runs ``write_vrt`` in a task.
``open_mosaic`` runs on the main thread. ``build_mosaic`` does all three
steps in the calling thread.

The VRT goes into the session folder of ``parquet_layer``, so the same
sweep deletes it when the last layer that reads it is gone. The layer keeps
the COG URLs. A saved project that lost its VRT reads a placeholder, and
``rebuild`` writes the VRT again from the URLs.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from osgeo import gdal, ogr
from qgis.core import QgsProviderRegistry, QgsRasterLayer

from portolan_registry_qgis.core import mirror as mirrors
from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core import scope as scopes
from portolan_registry_qgis.core.download import Walk, walk
from portolan_registry_qgis.core.i18n import _, ngettext, number
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.layers import (
    LayerError,
    blocked_message,
    explain_open_error,
    gdal_open_error,
    gdal_path,
    remote_reads,
)
from portolan_registry_qgis.qgis_io.log import log
from portolan_registry_qgis.qgis_io.network import run_task

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Executor

    from qgis.core import QgsTask

    from portolan_registry_qgis.core.download import FetchJson
    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Document

# The layer keeps the COG URLs here, one per line, so a saved project can
# build the VRT again.
SOURCES_PROPERTY = "portolan/mosaic_sources"
# The polygon that clips a mosaic, as WKT in EPSG:4326, so a saved project
# clips it again.
CLIP_PROPERTY = "portolan/mosaic_clip"
# The walk stops after this many documents.
MAX_DOCUMENTS = 20000


class MosaicCancelledError(LayerError):
    """The user cancelled the VRT while GDAL read the files."""


def item_hrefs_in(
    collection: Document,
    scope: Scope,
    fetch_json: FetchJson,
    executor: Executor | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[str]:
    """Return the COG hrefs of the collection's items that intersect ``scope``.

    A ``collection-mirror`` asset (stac-geoparquet) answers with one DuckDB
    query when DuckDB is present. Otherwise the items are read with
    ``download.walk`` and filtered by bbox. When the mirror cannot be read,
    the walk runs instead. Safe off the main thread.

    Each item gives its first COG data asset. An item with no COG adds
    nothing.
    """
    mirror = mirrors.mirror_href(collection)
    if mirror is not None and parquet_query.duckdb_status()[0]:
        import duckdb

        try:
            con = parquet_query.connection(parquet_layer.extension_directory())
            items = mirrors.item_documents(collection)
            return mirrors.mirror_hrefs(con, mirror, scope, cancelled, items)
        except (duckdb.Error, OSError, ValueError) as error:
            if cancelled is not None and cancelled():
                return []
            log(
                _("Could not read {url}, so the plugin reads each item: {error}").format(
                    url=mirror, error=error
                )
            )
    return walked_hrefs(collection.href, scope, fetch_json, executor, cancelled)


def walked_hrefs(
    start: str,
    scope: Scope,
    fetch_json: FetchJson,
    executor: Executor | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[str]:
    """Read every item below ``start`` and return the COGs of those in ``scope``."""
    failures: list[tuple[str, str]] = []
    hrefs: list[str] = []
    for document in walk(fetch_json, start, failures, Walk(MAX_DOCUMENTS, cancelled, executor)):
        if document.kind != "item" or not scopes.intersects(document.bbox, scope):
            continue
        href = mirrors.cog_href(document.assets)
        if href is not None:
            hrefs.append(href)
    if failures:
        log(
            ngettext(
                "Could not read {n} document below {start}, first {first}",
                "Could not read {n} documents below {start}, first {first}",
                len(failures),
            ).format(n=number(len(failures)), start=start, first=failures[0][0])
        )
    return list(dict.fromkeys(hrefs))


def write_vrt(
    hrefs: list[str],
    cancelled: Callable[[], bool] | None = None,
    clip: str | None = None,
) -> Path:
    """Write a VRT over the COGs at ``hrefs`` into the session folder.

    GDAL opens each COG to read its size and georeferencing, so this sends
    one or more requests per file. Safe off the main thread.

    With ``clip``, the file is a warped VRT that shows only the pixels
    inside the polygon, cropped to its extent. The warp keeps the CRS and
    the pixel grid of the COGs, so no pixel is resampled. The mosaic of the
    COGs goes inside the file as its source, so the layer reads one file.

    GDAL leaves out a file whose CRS or band count differs from the first
    file's, and a file it cannot open. The message log names each file left
    out and GDAL's reason, such as an HTTP status.

    Raises:
        MosaicCancelledError: ``cancelled`` returned True.
        LayerError: The list is empty, or GDAL cannot build the VRT.

    Args:
        hrefs: The COG URLs.
        cancelled: Polled while GDAL opens the files.
        clip: A Polygon or MultiPolygon in EPSG:4326 as WKT.
    """
    if not hrefs:
        raise LayerError(_("No file intersects the area"))
    path = parquet_layer.new_path(".vrt")
    try:
        _raise_if_blocked(hrefs)
        by_path = {gdal_path(h): h for h in hrefs}
        bands, error, skipped = _build_vrt(hrefs, path, cancelled, clip)
        reasons = _skip_reasons(skipped, by_path)
        if cancelled is not None and cancelled():
            # Translators: Why a task stopped. The user cancelled it.
            raise MosaicCancelledError(_("Cancelled"))
        if bands == 0 or not path.exists():
            raise _mosaic_error(len(hrefs), error, reasons)
    except BaseException:
        parquet_layer.discard_path(path)
        raise
    return path


def _raise_if_blocked(hrefs: list[str]) -> None:
    """Raise a ``LayerError`` when the GDAL settings block one of ``hrefs``.

    GDAL would draw nothing for such a file, so the error comes first.
    """
    blocked = next((message for h in hrefs if (message := blocked_message(h))), None)
    if blocked is not None:
        raise LayerError(blocked)


def _build_vrt(
    hrefs: list[str],
    path: Path,
    cancelled: Callable[[], bool] | None,
    clip: str | None,
) -> tuple[int, str, dict[str, str]]:
    """Let GDAL write the VRT over the COGs at ``hrefs`` at ``path``.

    Returns:
        The band count, which is 0 when GDAL wrote no VRT, GDAL's error,
        and GDAL's reason for each path it left out.
    """

    def progress(_complete: float, _message: object, _data: object) -> int:
        return 0 if cancelled is not None and cancelled() else 1

    options = gdal.BuildVRTOptions(callback=progress)
    paths = [gdal_path(h) for h in hrefs]
    # BuildVRT says only "Can't open <path>. Skipping it." for a file it
    # cannot open. The handler keeps those paths, to ask GDAL why.
    skipped: list[str] = []

    def keep(_level: int, _number: int, message: str) -> None:
        skip = mirrors.skipped_path(message)
        if skip is not None:
            skipped.append(skip)

    error = ""
    gdal.PushErrorHandler(keep)
    try:
        with remote_reads(hrefs):
            # A clipped mosaic builds the plain mosaic in memory first.
            target = "" if clip else str(path)
            dataset = gdal.BuildVRT(target, paths, options=options)
            bands = dataset.RasterCount if dataset is not None else 0
            if dataset is None:
                error = gdal.GetLastErrorMsg()
            elif clip:
                _warp(dataset, path, clip)
            # Closing the dataset writes the file.
            dataset = None
            reasons = {skip: gdal_open_error(skip) for skip in skipped}
    except RuntimeError as failure:
        return 0, str(failure), {}
    finally:
        gdal.PopErrorHandler()
    return bands, error, reasons


def _skip_reasons(skipped: dict[str, str], by_path: dict[str, str]) -> dict[str, str]:
    """Explain GDAL's reason for each path it left out, and log each one.

    Returns:
        The explained reason by URL.
    """
    reasons = {
        by_path.get(skip, skip): explain_open_error(by_path.get(skip, skip), reason)
        for skip, reason in skipped.items()
    }
    for url, reason in reasons.items():
        log(_("The mosaic left out {url}: {reason}").format(url=url, reason=reason))
    return reasons


def _mosaic_error(files: int, error: str, reasons: dict[str, str]) -> LayerError:
    """Return the error for a mosaic of ``files`` files that GDAL did not write.

    The first file GDAL left out, with its reason, replaces GDAL's own error.
    """
    if reasons:
        url, reason = next(iter(reasons.items()))
        error = _("GDAL could not open {url}: {reason}").format(url=url, reason=reason)
    return LayerError(
        ngettext(
            "GDAL could not build a mosaic of {n} file. {error}",
            "GDAL could not build a mosaic of {n} files. {error}",
            files,
        ).format(n=number(files), error=error)
    )


def _warp(mosaic: Any, path: Path, clip: str) -> None:
    """Write a warped VRT of ``mosaic`` at ``path`` that ``clip`` cuts and crops.

    GDAL reads the cutline from a GeoJSON file in memory, and keeps the
    polygon in the VRT. A band with no nodata value gets an alpha band, so
    the pixels outside the polygon draw as transparent.
    """
    shape = ogr.CreateGeometryFromWkt(clip)
    if shape is None:
        raise RuntimeError(_("The clip is not WKT"))
    cutline = f"/vsimem/portolan-clip-{uuid.uuid4().hex}.geojson"
    geometry = json.loads(shape.ExportToJson())
    feature = {"type": "Feature", "properties": {}, "geometry": geometry}  # i18n: skip - GeoJSON
    gdal.FileFromMemBuffer(
        cutline, json.dumps({"type": "FeatureCollection", "features": [feature]})
    )
    # The plain mosaic lives in memory. Its XML text is a dataset name that
    # GDAL opens, so the warped VRT keeps the mosaic inline.
    source = gdal.Open(mosaic.GetMetadata("xml:VRT")[0])
    nodata = source.GetRasterBand(1).GetNoDataValue()
    options = gdal.WarpOptions(
        format="VRT",
        # GeoJSON is in longitude and latitude by its specification, so the
        # cutline needs no CRS of its own. GDAL 3.8 has no cutlineSRS option.
        cutlineDSName=cutline,
        cropToCutline=True,
        dstSRS=source.GetProjection(),
        dstNodata=nodata,
        dstAlpha=nodata is None,
    )
    try:
        warped = gdal.Warp(str(path), source, options=options)
        if warped is None:
            raise RuntimeError(gdal.GetLastErrorMsg() or _("GDAL could not clip the mosaic"))
        warped = None
    finally:
        gdal.Unlink(cutline)


def open_mosaic(path: Path, hrefs: list[str], name: str, clip: str | None = None) -> QgsRasterLayer:
    """Open a VRT that ``write_vrt`` wrote as a layer. Call on the main thread.

    Raises:
        LayerError: QGIS cannot open the VRT.
    """
    layer = QgsRasterLayer(str(path), name, "gdal")
    if not layer.isValid():
        parquet_layer.discard_path(path)
        raise LayerError(
            _("QGIS could not open the mosaic: {error}").format(error=layer.error().summary())
        )
    parquet_layer.release_path(path)
    layer.setCustomProperty(SOURCES_PROPERTY, "\n".join(hrefs))
    if clip:
        layer.setCustomProperty(CLIP_PROPERTY, clip)
    return layer


def build_mosaic(hrefs: list[str], name: str, clip: str | None = None) -> QgsRasterLayer:
    """Write a VRT over ``hrefs`` to the scratch folder and open it as a layer.

    Raises:
        layers.LayerError: GDAL cannot build the VRT, or QGIS cannot open it.
    """
    return open_mosaic(write_vrt(hrefs, clip=clip), hrefs, name, clip)


def clip_of(layer: object) -> str | None:
    """Return the WKT that clips a mosaic layer, or None when it shows whole files."""
    if not isinstance(layer, QgsRasterLayer):
        return None
    return str(layer.customProperty(CLIP_PROPERTY) or "") or None


def sources(layer: object) -> list[str]:
    """Return the COG URLs a mosaic layer keeps, or nothing for another layer."""
    if not isinstance(layer, QgsRasterLayer):
        return []
    text = str(layer.customProperty(SOURCES_PROPERTY) or "")
    return [line for line in text.splitlines() if line]


def needs_rebuild(layer: object) -> bool:
    """Return whether ``layer`` is a mosaic whose VRT is gone.

    With ``parquet_layer.restore_path`` in place, such a layer reads the
    placeholder VRT. On QGIS 3.34 it keeps the path of the missing VRT and
    opens as unavailable.
    """
    if not sources(layer) or not isinstance(layer, QgsRasterLayer):
        return False
    if layer.providerType() != "gdal":
        return False
    decoded = QgsProviderRegistry.instance().decodeUri("gdal", layer.source()).get("path")
    if not decoded:
        return False
    return parquet_layer.is_lost_copy(Path(decoded))


def reopen(layer: QgsRasterLayer, path: Path) -> None:
    """Point a mosaic layer at a VRT that ``write_vrt`` wrote. Call on the main thread.

    The layer keeps its name, its style, and its custom properties.

    Raises:
        LayerError: QGIS cannot open the VRT.
    """
    layer.setDataSource(str(path), layer.name(), "gdal")
    if not layer.isValid():
        parquet_layer.discard_path(path)
        raise LayerError(_("QGIS could not open the mosaic {path}").format(path=path))
    parquet_layer.release_path(path)
    layer.triggerRepaint()


def rebuild(layer: QgsRasterLayer, cancelled: Callable[[], bool] | None = None) -> None:
    """Write the VRT of a saved mosaic again from ``SOURCES_PROPERTY``.

    The function sends network requests and opens the layer, so it blocks
    the main thread. To keep the window responsive, call ``write_vrt`` with
    ``sources(layer)`` in a task, then ``reopen`` on the main thread.

    Raises:
        LayerError: The layer keeps no URLs, or GDAL or QGIS cannot open them.
    """
    hrefs = sources(layer)
    if not hrefs:
        raise LayerError(_("{layer} keeps no mosaic sources").format(layer=layer.name()))
    reopen(layer, write_vrt(hrefs, cancelled, clip_of(layer)))


def start_vrt(
    description: str,
    hrefs: list[str],
    clip: str | None,
    on_done: Callable[[Path | None, BaseException | None], None],
) -> QgsTask:
    """Run ``write_vrt`` in a background task. Call on the main thread.

    Cancel on the task stops the write.

    Args:
        description: Shown in the QGIS task manager.
        hrefs: The COG URLs, as for ``write_vrt``.
        clip: The clip polygon, as for ``write_vrt``.
        on_done: Called on the main thread with the path of the VRT and
            None, or with None and the error.

    Returns:
        The task, already queued.
    """

    def write(task: QgsTask) -> Path:
        return write_vrt(hrefs, task.isCanceled, clip)

    def done(result: object, error: BaseException | None) -> None:
        on_done(result if isinstance(result, Path) else None, error)

    return run_task(description, write, done)
