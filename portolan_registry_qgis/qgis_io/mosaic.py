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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from osgeo import gdal
from qgis.core import Qgis, QgsMessageLog, QgsProviderRegistry, QgsRasterLayer

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core import scope as scopes
from portolan_registry_qgis.core.download import walk
from portolan_registry_qgis.core.geoparquet import quote
from portolan_registry_qgis.core.hrefs import asset_href, vsicurl_path
from portolan_registry_qgis.core.stac import asset_format
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.layers import LayerError

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Executor

    from portolan_registry_qgis.core.download import FetchJson
    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Bbox, Document

# The layer keeps the COG URLs here, one per line, so a saved project can
# build the VRT again.
SOURCES_PROPERTY = "portolan/mosaic_sources"
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
            return mirror_hrefs(mirror, scope, cancelled)
        except (duckdb.Error, OSError, ValueError) as error:
            if cancelled is not None and cancelled():
                return []
            _log(f"Could not read {mirror}, so the plugin reads each item: {error}")
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
    for document in walk(fetch_json, start, failures, MAX_DOCUMENTS, cancelled, executor):
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
        _log(f"Could not read {len(failures)} documents below {start}, first {failures[0][0]}")
    return list(dict.fromkeys(hrefs))


def _bbox_columns(kind: str) -> tuple[str, str, str, str]:
    """Return the SQL that reads xmin, ymin, xmax, and ymax from the bbox column.

    stac-geoparquet 1.0 stores a struct with named fields, as GeoParquet 1.1
    defines. Earlier files store a list of four or six numbers.
    """
    if kind.upper().startswith("STRUCT"):
        return ("bbox.xmin", "bbox.ymin", "bbox.xmax", "bbox.ymax")
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
        raise ValueError("The file has no bbox or assets column")
    xmin, ymin, xmax, ymax = _bbox_columns(columns["bbox"])
    links = "to_json(links)" if "links" in columns else "NULL"
    sql = (
        f"SELECT {xmin}, {ymin}, {xmax}, {ymax}, to_json(assets), {links} "  # noqa: S608  # nosec B608 - the SQL holds column names only
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
) -> list[str]:
    """Return the COG hrefs of the items of a stac-geoparquet file that intersect ``scope``.

    A relative asset href resolves against the item's ``self`` link, or
    against the file when the item has no such link.

    Raises:
        parquet_query.DuckDBMissingError: DuckDB is absent or too old.
        duckdb.Error: DuckDB cannot read the file.
        ValueError: The file is not stac-geoparquet.
    """
    con = parquet_query.connection(parquet_layer.extension_directory())
    # The cursor stops the running query when ``cancelled`` returns True.
    with parquet_query._cursor(con, cancelled) as cursor:
        described = cursor.execute("DESCRIBE SELECT * FROM read_parquet(?)", [url]).fetchall()
        columns = {str(row[0]): str(row[1]) for row in described}
        sql, params = mirror_query(columns, scope)
        rows = cursor.execute(sql, [url, *params]).fetchall()
    hrefs = []
    for xmin, ymin, xmax, ymax, assets, links in rows:
        corners = (xmin, ymin, xmax, ymax)
        box: Bbox | None = None
        if all(isinstance(value, (int, float)) for value in corners):
            box = (float(xmin), float(ymin), float(xmax), float(ymax))
        if not scopes.intersects(box, scope):
            continue
        href = _first_cog(_json(assets), _self_href(_json(links), url))
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


def _self_href(links: object, base: str) -> str:
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and link.get("rel") == "self":
                href = link.get("href")
                if isinstance(href, str) and href:
                    try:
                        return asset_href(href, base)
                    except ValueError:
                        return base
    return base


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


def write_vrt(hrefs: list[str], cancelled: Callable[[], bool] | None = None) -> Path:
    """Write a VRT over the COGs at ``hrefs`` into the session folder.

    GDAL opens each COG to read its size and georeferencing, so this sends
    one or more requests per file. Safe off the main thread.

    GDAL leaves out a file whose CRS or band count differs from the first
    file's.

    Raises:
        MosaicCancelledError: ``cancelled`` returned True.
        LayerError: The list is empty, or GDAL cannot build the VRT.
    """
    if not hrefs:
        raise LayerError("No file intersects the area")
    path = parquet_layer.new_path(".vrt")

    def progress(_complete: float, _message: object, _data: object) -> int:
        return 0 if cancelled is not None and cancelled() else 1

    options = gdal.BuildVRTOptions(callback=progress)
    error = ""
    try:
        dataset = gdal.BuildVRT(str(path), [vsicurl_path(h) for h in hrefs], options=options)
        bands = dataset.RasterCount if dataset is not None else 0
        if dataset is None:
            error = gdal.GetLastErrorMsg()
        # Closing the dataset writes the file.
        dataset = None
    except RuntimeError as failure:
        bands, error = 0, str(failure)
    if cancelled is not None and cancelled():
        parquet_layer.discard_path(path)
        raise MosaicCancelledError("Cancelled")
    if bands == 0 or not path.exists():
        parquet_layer.discard_path(path)
        raise LayerError(f"GDAL could not build a mosaic of {len(hrefs)} files: {error}")
    return path


def open_mosaic(path: Path, hrefs: list[str], name: str) -> QgsRasterLayer:
    """Open a VRT that ``write_vrt`` wrote as a layer. Call on the main thread.

    Raises:
        LayerError: QGIS cannot open the VRT.
    """
    layer = QgsRasterLayer(str(path), name, "gdal")
    if not layer.isValid():
        parquet_layer.discard_path(path)
        raise LayerError(f"QGIS could not open the mosaic: {layer.error().summary()}")
    parquet_layer.release_path(path)
    layer.setCustomProperty(SOURCES_PROPERTY, "\n".join(hrefs))
    return layer


def build_mosaic(hrefs: list[str], name: str) -> QgsRasterLayer:
    """Write a VRT over ``hrefs`` to the scratch folder and open it as a layer.

    Raises:
        layers.LayerError: GDAL cannot build the VRT, or QGIS cannot open it.
    """
    return open_mosaic(write_vrt(hrefs), hrefs, name)


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
        raise LayerError(f"QGIS could not open the mosaic {path}")
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
        raise LayerError(f"{layer.name()} keeps no mosaic sources")
    reopen(layer, write_vrt(hrefs, cancelled))


def _log(text: str) -> None:
    QgsMessageLog.logMessage(text, "Portolan Registry", Qgis.MessageLevel.Warning)
