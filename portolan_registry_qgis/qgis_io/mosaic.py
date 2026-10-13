"""Add many COG files as one raster layer.

A collection of COG items becomes a GDAL VRT over the ``/vsicurl/`` paths
of the items that intersect the scope. QGIS reads only the tiles on screen.

``item_hrefs_in`` and ``write_vrt`` send network requests, so they run off
the main thread. ``open_mosaic`` runs on the main thread. ``build_mosaic``
does all three steps in the calling thread.

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
from urllib.parse import urlsplit

from osgeo import gdal, ogr
from qgis.core import Qgis, QgsMessageLog, QgsProviderRegistry, QgsRasterLayer

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core import scope as scopes
from portolan_registry_qgis.core.download import Walk, walk
from portolan_registry_qgis.core.geoparquet import quote
from portolan_registry_qgis.core.hrefs import asset_href
from portolan_registry_qgis.core.i18n import _, ngettext, number
from portolan_registry_qgis.core.stac import asset_format
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.layers import (
    LayerError,
    blocked_message,
    explain_open_error,
    gdal_open_error,
    gdal_path,
    remote_reads,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Executor

    from portolan_registry_qgis.core.download import FetchJson
    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Bbox, Document

# The layer keeps the COG URLs here, one per line, so a saved project can
# build the VRT again.
SOURCES_PROPERTY = "portolan/mosaic_sources"
# The polygon that clips a mosaic, as WKT in EPSG:4326, so a saved project
# clips it again.
CLIP_PROPERTY = "portolan/mosaic_clip"
# The role of a collection asset that holds every item as stac-geoparquet.
MIRROR_ROLE = "collection-mirror"
# The walk stops after this many documents.
MAX_DOCUMENTS = 20000
# Asset roles that do not hold the data, as in ``Document.data_assets``.
_NOT_DATA = frozenset({"thumbnail", "overview", "metadata"})


class MosaicCancelledError(LayerError):
    """The user cancelled the VRT while GDAL read the files."""


def mirror_href(collection: Document) -> str | None:
    """Return the href of the collection's stac-geoparquet mirror, if it has one."""
    return next(
        (asset.href for asset in collection.assets if MIRROR_ROLE in asset.roles),
        None,
    )


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
    mirror = mirror_href(collection)
    if mirror is not None and parquet_query.duckdb_status()[0]:
        import duckdb

        try:
            return mirror_hrefs(mirror, scope, cancelled, item_documents(collection))
        except (duckdb.Error, OSError, ValueError) as error:
            if cancelled is not None and cancelled():
                return []
            _log(
                _("Could not read {url}, so the plugin reads each item: {error}").format(
                    url=mirror, error=error
                )
            )
    return walked_hrefs(collection.href, scope, fetch_json, executor, cancelled)


def item_documents(collection: Document) -> dict[str, str]:
    """Map each item ID to the URL of its document, from the collection's item links.

    A link names no ID, so the ID is the file name of the item document
    without ``.json``. This fits the usual layouts ``<id>/<id>.json`` and
    ``<id>.json``.
    """
    found: dict[str, str] = {}
    for node in collection.children:
        if node.kind != "item":
            continue
        name = urlsplit(node.href).path.rsplit("/", 1)[-1]
        found.setdefault(name.removesuffix(".json"), node.href)
    return found


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
        href = next(
            (
                asset.href
                for asset in document.data_assets
                if asset.format == "cog" and not _NOT_DATA.intersection(asset.roles)
            ),
            None,
        )
        if href is not None:
            hrefs.append(href)
    if failures:
        _log(
            ngettext(
                "Could not read {n} document below {start}, first {first}",
                "Could not read {n} documents below {start}, first {first}",
                len(failures),
            ).format(n=number(len(failures)), start=start, first=failures[0][0])
        )
    return list(dict.fromkeys(hrefs))


def _bbox_columns(kind: str) -> tuple[str, str, str, str]:
    """Return the SQL that reads xmin, ymin, xmax, and ymax from the bbox column.

    stac-geoparquet 1.0 stores a struct with named fields, as GeoParquet 1.1
    defines. Earlier files store a list of four or six numbers.
    """
    if kind.upper().startswith("STRUCT"):
        return ("bbox.xmin", "bbox.ymin", "bbox.xmax", "bbox.ymax")  # i18n: skip - SQL
    half = "len(bbox) // 2"
    return ("bbox[1]", "bbox[2]", f"bbox[{half} + 1]", f"bbox[{half} + 2]")


def mirror_query(columns: dict[str, str], scope: Scope) -> tuple[str, list[Any]]:
    """Build the DuckDB query that reads the items of a stac-geoparquet file.

    The query keeps the rows whose bbox can intersect the scope's envelope.
    A row whose bbox crosses the antimeridian stays, and the caller tests
    it. An ``Area`` scope also tests the item geometry against the polygon.

    Args:
        columns: The file's column names and DuckDB types.
        scope: The part of the collection to keep.

    Returns:
        The SQL, with ``?`` for the file URL first, and the other parameters.

    Raises:
        ValueError: The file has no ``bbox`` or ``assets`` column.
    """
    if "bbox" not in columns or "assets" not in columns:
        raise ValueError(_("The file has no bbox or assets column"))
    xmin, ymin, xmax, ymax = _bbox_columns(columns["bbox"])
    links = "to_json(links)" if "links" in columns else "NULL"
    ids = quote("id") if "id" in columns else "NULL"
    sql = (
        f"SELECT {xmin}, {ymin}, {xmax}, {ymax}, to_json(assets), {links}, {ids} "  # noqa: S608  # nosec B608 - the SQL holds column names only
        "FROM read_parquet(?)"
    )
    params: list[Any] = []
    box = scopes.bbox_of(scope)
    if box is None or box[0] > box[2]:
        return sql, params
    west, south, east, north = box
    where = [
        f"({xmin} > {xmax} OR ({xmax} >= ? AND {xmin} <= ?))",
        f"{ymax} >= ?",
        f"{ymin} <= ?",
    ]
    params += [west, east, south, north]
    geometry = columns.get("geometry", "").upper()
    if isinstance(scope, scopes.Area) and geometry.startswith(("GEOMETRY", "BLOB")):
        column = quote("geometry")
        shape = column if geometry.startswith("GEOMETRY") else f"ST_GeomFromWKB({column})"
        where.append(f"({xmin} > {xmax} OR ST_Intersects({shape}, ST_GeomFromText(?)))")
        params.append(scope.wkt)
    return f"{sql} WHERE {' AND '.join(where)}", params


def mirror_hrefs(
    url: str,
    scope: Scope,
    cancelled: Callable[[], bool] | None = None,
    items: dict[str, str] | None = None,
) -> list[str]:
    """Return the COG hrefs of the items of a stac-geoparquet file that intersect ``scope``.

    A relative asset href is relative to its item document. The function
    finds that document from the item's ``self`` link, else from ``items``
    by the item ID, else it uses the file. A stac-geoparquet file often
    leaves out the ``self`` link, as the GHSL mirror does.

    Args:
        url: The stac-geoparquet file.
        scope: The part of the collection to keep.
        cancelled: Polled while the query runs.
        items: The URL of each item document by item ID, from
            ``item_documents``.

    Raises:
        parquet_query.DuckDBMissingError: DuckDB is absent or too old.
        duckdb.Error: DuckDB cannot read the file.
        ValueError: The file is not stac-geoparquet.
    """
    con = parquet_query.connection(parquet_layer.extension_directory())
    # The cursor stops the running query when ``cancelled`` returns True.
    with parquet_query.cursor(con, cancelled) as cursor:
        described = cursor.execute("DESCRIBE SELECT * FROM read_parquet(?)", [url]).fetchall()
        columns = {str(row[0]): str(row[1]) for row in described}
        sql, params = mirror_query(columns, scope)
        rows = cursor.execute(sql, [url, *params]).fetchall()
    hrefs = []
    for xmin, ymin, xmax, ymax, assets, links, item_id in rows:
        corners = (xmin, ymin, xmax, ymax)
        box: Bbox | None = None
        if all(isinstance(value, (int, float)) for value in corners):
            box = (float(xmin), float(ymin), float(xmax), float(ymax))
        if not scopes.intersects(box, scope):
            continue
        base = _self_href(_json(links), url)
        if base is None:
            base = (items or {}).get(str(item_id), url) if item_id is not None else url
        href = _first_cog(_json(assets), base)
        if href is not None:
            hrefs.append(href)
    return list(dict.fromkeys(hrefs))


def _json(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _self_href(links: object, base: str) -> str | None:
    """Return the item's ``self`` link resolved against ``base``, or None without one."""
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("rel") == "self":
                href = link.get("href")
                if isinstance(href, str) and href:
                    try:
                        return asset_href(href, base)
                    except ValueError:
                        return None
    return None


def _first_cog(assets: object, base: str) -> str | None:
    if not isinstance(assets, dict):
        return None
    for asset in assets.values():
        if not isinstance(asset, dict) or not isinstance(asset.get("href"), str):
            continue
        roles = asset.get("roles")
        if isinstance(roles, list) and _NOT_DATA.intersection(
            r for r in roles if isinstance(r, str)
        ):
            continue
        try:
            href = asset_href(asset["href"], base)
        except ValueError:
            continue
        media = asset.get("type")
        if asset_format(href, media if isinstance(media, str) else None) == "cog":
            return href
    return None


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

    def progress(_complete: float, _message: object, _data: object) -> int:
        return 0 if cancelled is not None and cancelled() else 1

    options = gdal.BuildVRTOptions(callback=progress)
    # A file that the GDAL settings block would draw nothing. Say so first.
    blocked = next((message for h in hrefs if (message := blocked_message(h))), None)
    if blocked is not None:
        parquet_layer.discard_path(path)
        raise LayerError(blocked)
    paths = [gdal_path(h) for h in hrefs]
    error = ""
    # BuildVRT says only "Can't open <path>. Skipping it." for a file it
    # cannot open. The handler keeps those paths, to ask GDAL why.
    skipped: list[str] = []

    def keep(_level: int, _number: int, message: str) -> None:
        # GDAL writes this message in English, so it stays English here.
        if message.startswith("Can't open ") and message.endswith(". Skipping it"):  # i18n: skip
            skipped.append(message[len("Can't open ") : -len(". Skipping it")])

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
        bands, error, reasons = 0, str(failure), {}
    finally:
        gdal.PopErrorHandler()
    by_path = dict(zip(paths, hrefs, strict=True))
    for skip, reason in list(reasons.items()):
        reasons[skip] = explain_open_error(by_path.get(skip, skip), reason)
    for skip, reason in reasons.items():
        _log(
            _("The mosaic left out {url}: {reason}").format(
                url=by_path.get(skip, skip), reason=reason
            )
        )
    if cancelled is not None and cancelled():
        parquet_layer.discard_path(path)
        # Translators: Why a task stopped. The user cancelled it.
        raise MosaicCancelledError(_("Cancelled"))
    if bands == 0 or not path.exists():
        parquet_layer.discard_path(path)
        if reasons:
            first, reason = next(iter(reasons.items()))
            error = _("GDAL could not open {url}: {reason}").format(
                url=by_path.get(first, first), reason=reason
            )
        raise LayerError(
            ngettext(
                "GDAL could not build a mosaic of {n} file. {error}",
                "GDAL could not build a mosaic of {n} files. {error}",
                len(hrefs),
            ).format(n=number(len(hrefs)), error=error)
        )
    return path


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


def _log(text: str) -> None:
    QgsMessageLog.logMessage(text, "Portolan Registry", Qgis.MessageLevel.Warning)
