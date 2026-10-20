"""Add the COG files of a collection's items as one GDAL VRT raster layer.

The VRT goes into the session folder of ``parquet_layer``. The layer keeps
the COG URLs, so ``rebuild`` can write a lost VRT again.
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
    """Return the file of each item of ``collection`` in ``scope``: its COG, else its GeoParquet.

    A ``collection-mirror`` asset answers through DuckDB when it can. Else the
    function walks the items. Safe off the main thread.
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
    """Read every item below ``start`` and return the files of those in ``scope``."""
    failures: list[tuple[str, str]] = []
    hrefs: list[str] = []
    for document in walk(fetch_json, start, failures, Walk(MAX_DOCUMENTS, cancelled, executor)):
        if document.kind != "item" or not scopes.intersects(document.bbox, scope):
            continue
        href = mirrors.item_href(document.assets)
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
    progress: Callable[[float], None] | None = None,
) -> Path:
    """Write a VRT over the COGs at ``hrefs`` into the session folder. Safe off the main thread.

    ``clip`` is EPSG:4326 WKT. With it, a warped VRT crops to the polygon and
    keeps the pixel grid. ``progress`` gets the share of the COGs GDAL has opened.
    Raise MosaicCancelledError on cancel, and LayerError when GDAL fails.
    """
    if not hrefs:
        raise LayerError(_("No file intersects the area"))
    path = parquet_layer.new_path(".vrt")
    try:
        _write_vrt_at(hrefs, path, cancelled, clip, progress)
    except BaseException:
        parquet_layer.discard_path(path)
        raise
    return path


def _write_vrt_at(
    hrefs: list[str],
    path: Path,
    cancelled: Callable[[], bool] | None,
    clip: str | None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Write the VRT of ``write_vrt`` at ``path``, and raise when it fails.

    Raises:
        MosaicCancelledError: ``cancelled`` returned True.
        LayerError: The GDAL settings block a file, or GDAL cannot build the VRT.
    """
    _raise_if_blocked(hrefs)
    by_path = {gdal_path(h): h for h in hrefs}
    bands, error, skipped = _build_vrt(hrefs, path, cancelled, clip, progress)
    reasons = _skip_reasons(skipped, by_path)
    if cancelled is not None and cancelled():
        # Translators: Why a task stopped. The user cancelled it.
        raise MosaicCancelledError(_("Cancelled"))
    if bands == 0 or not path.exists():
        raise _mosaic_error(len(hrefs), error, reasons)


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
    progress: Callable[[float], None] | None = None,
) -> tuple[int, str, dict[str, str]]:
    """Let GDAL write the VRT over the COGs at ``hrefs`` at ``path``.

    Returns:
        The band count, which is 0 when GDAL wrote no VRT, GDAL's error,
        and GDAL's reason for each path it left out.
    """

    def report(complete: float, _message: object, _data: object) -> int:
        if progress is not None:
            progress(complete)
        return 0 if cancelled is not None and cancelled() else 1

    options = gdal.BuildVRTOptions(callback=report)
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


def clip_geojson(clip: str) -> str:
    """Return the WKT polygon ``clip`` as a GeoJSON FeatureCollection, the cutline GDAL reads.

    Raises:
        RuntimeError: ``clip`` is not WKT.
    """
    shape = ogr.CreateGeometryFromWkt(clip)
    if shape is None:
        raise RuntimeError(_("The clip is not WKT"))
    geometry = json.loads(shape.ExportToJson())
    feature = {"type": "Feature", "properties": {}, "geometry": geometry}  # i18n: skip - GeoJSON
    return json.dumps({"type": "FeatureCollection", "features": [feature]})


def _warp(mosaic: Any, path: Path, clip: str) -> None:
    """Write a warped VRT of ``mosaic`` at ``path`` that ``clip`` cuts and crops.

    GDAL reads the cutline from a GeoJSON file in memory, and keeps the
    polygon in the VRT. A band with no nodata value gets an alpha band, so
    the pixels outside the polygon draw as transparent.
    """
    cutline = f"/vsimem/portolan-clip-{uuid.uuid4().hex}.geojson"
    gdal.FileFromMemBuffer(cutline, clip_geojson(clip))
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

    The layer keeps its name, style, and properties. Raise LayerError when
    QGIS cannot open the VRT.
    """
    layer.setDataSource(str(path), layer.name(), "gdal")
    if not layer.isValid():
        parquet_layer.discard_path(path)
        raise LayerError(_("QGIS could not open the mosaic {path}").format(path=path))
    parquet_layer.release_path(path)
    layer.triggerRepaint()


def rebuild(layer: QgsRasterLayer, cancelled: Callable[[], bool] | None = None) -> None:
    """Write the VRT of a saved mosaic again from ``SOURCES_PROPERTY``, and reopen it.

    This blocks on the network. In the window, run ``write_vrt`` in a task
    and then ``reopen``. Raise LayerError when the layer keeps no URLs.
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
    """Run ``write_vrt`` in a queued background task, and return the task.

    ``on_done`` gets the VRT path or the error on the main thread. Cancel on
    the task stops the write.
    """

    def write(task: QgsTask) -> Path:
        return write_vrt(hrefs, task.isCanceled, clip, lambda done: task.setProgress(done * 100))

    def done(result: object, error: BaseException | None) -> None:
        on_done(result if isinstance(result, Path) else None, error)

    return run_task(description, write, done)
