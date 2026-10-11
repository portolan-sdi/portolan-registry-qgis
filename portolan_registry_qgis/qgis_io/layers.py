"""Turn STAC assets and PMTiles links into QGIS layers.

PMTiles become native vector tile layers through the loopback tile server.
Each layer carries every collection MapLibre style that draws its archive, as
a named QGIS style. GeoJSON, FlatGeobuf, and CSV open through OGR,
COGs through GDAL, and COPC through the point cloud provider, all over HTTP
range requests without a download. GeoParquet goes through DuckDB instead, in
``parquet_layer``.
"""

from __future__ import annotations

import contextlib
import csv
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from osgeo import gdal
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsDataSourceUri,
    QgsMapBoxGlStyleConversionContext,
    QgsMapBoxGlStyleConverter,
    QgsMapLayer,
    QgsMapLayerStyle,
    QgsPointCloudLayer,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsVectorTileLayer,
)

from portolan_registry_qgis.core.diagnose import open_failure
from portolan_registry_qgis.core.hrefs import (
    extension_blocked,
    plain_vsicurl_path,
    url_extension,
    vsicurl_path,
)
from portolan_registry_qgis.core.parquet_query import is_flatpak
from portolan_registry_qgis.qgis_io.network import NetworkError, fetch_range, http_status
from portolan_registry_qgis.qgis_io.tileserver import Archive, TileServer, open_archive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from portolan_registry_qgis.core.stac import Asset, Document

PMTILES_PROPERTY = "portolan/pmtiles_url"
DEFAULT_STYLE_NAME = "QGIS default style"
_PMTILES_SCHEME = "pmtiles://"
# Header names that hold point coordinates in EPSG:4326, in order of
# preference. The comparison ignores case.
LONGITUDE_NAMES = ("lon", "lng", "longitude", "x")
LATITUDE_NAMES = ("lat", "latitude", "y")
# Many CSVs use x and y for projected coordinates. These names count only
# when the rows in the header bytes hold longitudes and latitudes.
_PLANAR_NAMES = frozenset({"x", "y"})
# The first request for a CSV reads this many bytes, enough for the header.
CSV_HEADER_BYTES = 16384
# The separators that GDAL's CSV driver detects in the header line.
_CSV_SEPARATORS = (",", ";", "\t", "|")


class LayerError(RuntimeError):
    """QGIS could not open the asset as a layer."""


# GDAL retries HTTP 429, 500, 502, 503, and 504 with these settings. A
# setting that the user gave in the QGIS options wins.
_HTTP_RETRY = {"GDAL_HTTP_MAX_RETRY": "3", "GDAL_HTTP_RETRY_DELAY": "1"}


_ALLOWED = "CPL_VSIL_CURL_ALLOWED_EXTENSIONS"


def gdal_path(url: str) -> str:
    """Return the GDAL path that reads ``url`` in this QGIS session.

    The path with the ``empty_dir`` option opens faster. GDAL refuses that
    path when ``CPL_VSIL_CURL_ALLOWED_EXTENSIONS`` is set, as a QGIS 3.40
    session with the value ``TIF`` showed. The plain path works then.
    """
    if gdal.GetConfigOption(_ALLOWED):
        return plain_vsicurl_path(url)
    return vsicurl_path(url)


def blocked_message(url: str) -> str | None:
    """Return why the GDAL settings keep the reads of ``url`` from working, or None."""
    setting = gdal.GetConfigOption(_ALLOWED)
    if not extension_blocked(url, setting):
        return None
    extension = url_extension(url) or "files with no extension"
    return (
        f"QGIS could not open {url}. The GDAL setting {_ALLOWED} is {setting!r}, "
        f"so GDAL reads no {extension} file over HTTP. Remove the setting, or add "
        f"{extension} to it, in Settings > Options, under System > Environment or GDAL. "
        "Then restart QGIS."
    )


@contextlib.contextmanager
def remote_reads(urls: list[str]) -> Iterator[None]:
    """Open the files at ``urls`` with retries, and without a remembered failure.

    GDAL keeps the result of a failed request for the life of the process.
    One busy moment on the server then fails every later open of the file
    until QGIS restarts. The function drops that result for ``urls``. The retry
    settings apply to the calling thread only.
    """
    clear = getattr(gdal, "VSICurlPartialClearCache", None)
    if clear is not None:
        for url in urls:
            clear(gdal_path(url))
    earlier = {key: gdal.GetThreadLocalConfigOption(key, None) for key in _HTTP_RETRY}
    for key, value in _HTTP_RETRY.items():
        if gdal.GetConfigOption(key) is None:
            gdal.SetThreadLocalConfigOption(key, value)
    try:
        yield
    finally:
        for key, value in earlier.items():
            gdal.SetThreadLocalConfigOption(key, value)


# GDAL gives this text for a /vsicurl path it cannot read, whatever the
# reason. A 404, a refused path, and a network block all read the same.
_GENERIC = "does not exist in the file system"


def explain_open_error(url: str, message: str) -> str:
    """Return a reason that says why GDAL could not read ``url``.

    When GDAL gives only its generic text, the function looks for the cause.
    It checks the GDAL settings first, then asks for the file through the
    QGIS network.
    """
    if _GENERIC not in message:
        return message
    blocked = blocked_message(url)
    if blocked is not None:
        return blocked
    status, error = http_status(url)
    if status is not None and status >= 400:
        return f"the server answered HTTP {status}"
    if status is not None:
        return (
            "QGIS reads the file, but GDAL does not. A GDAL network setting, such as a "
            "proxy or a certificate setting in Settings > Options, may block GDAL"
        )
    return error or message


def gdal_open_error(path: str) -> str:
    """Open ``path`` with GDAL and return its error, or "" when it opens.

    The error handler and the last error are local to the thread, so the
    function does not change what other threads report.
    """
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        gdal.ErrorReset()
        try:
            dataset = gdal.Open(path)
        except RuntimeError as error:
            return str(error)
        if dataset is not None:
            return ""
        return gdal.GetLastErrorMsg() or "GDAL gave no reason"
    finally:
        gdal.PopErrorHandler()


@dataclass
class Styled:
    """A style split per PMTiles source, ready for ``QgsMapBoxGlStyleConverter``."""

    by_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def archive_urls(document: Document) -> list[str]:
    """Return the collection's PMTiles archives, from its links and its assets."""
    urls = [link.href for link in document.pmtiles]
    urls += [asset.href for asset in document.assets if asset.format == "pmtiles"]
    return list(dict.fromkeys(urls))


def pmtiles_sources(
    style: dict[str, Any], style_url: str, fallback: str | None = None
) -> dict[str, str]:
    """Map each PMTiles source id in a MapLibre style to an absolute archive URL.

    A Portolan style names its archive in ``sources.<id>.url``, relative to the
    style file. Catalogs write the path bare (``../x.pmtiles``) or with the
    ``pmtiles://`` scheme, so the function accepts both. A vector source with
    neither ``url`` nor ``tiles`` reads ``fallback``, the collection's only
    archive.
    """
    sources = style.get("sources")
    found: dict[str, str] = {}
    if not isinstance(sources, dict):
        return found
    for source_id, source in sources.items():
        if not isinstance(source, dict) or source.get("type") != "vector":
            continue
        url = source.get("url")
        if url is None and "tiles" not in source:
            if fallback is not None:
                found[str(source_id)] = fallback
            continue
        if not isinstance(url, str):
            continue
        if url.startswith(_PMTILES_SCHEME):
            url = url[len(_PMTILES_SCHEME) :]
        elif not urlsplit(url).path.lower().endswith(".pmtiles"):
            continue
        found[str(source_id)] = urljoin(style_url, url)
    return found


def split_style(style: dict[str, Any], style_url: str, fallback: str | None = None) -> Styled:
    """Split a MapLibre style into one style per PMTiles source.

    QGIS draws one archive per vector tile layer, so each archive gets the
    style layers that read from it. Background layers go with the first
    archive. Layers on any other source are dropped with a warning.
    """
    sources = pmtiles_sources(style, style_url, fallback)
    result = Styled()
    raw_layers = style.get("layers")
    layers = raw_layers if isinstance(raw_layers, list) else []
    first = next(iter(sources.values()), None)
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        source = layer.get("source")
        if layer.get("type") == "background" and first is not None:
            url: str | None = first
        else:
            url = sources.get(str(source)) if source is not None else None
        if url is None:
            result.warnings.append(
                f"Style layer {layer.get('id')!r} reads source {source!r}, "
                "which is not a PMTiles archive; skipped."
            )
            continue
        target = result.by_source.setdefault(url, {**style, "layers": []})
        target["layers"].append(layer)
    return result


def convert_style(
    style: dict[str, Any],
    sprite: tuple[Any, dict[str, Any]] | None = None,
) -> tuple[Any, Any, list[str]]:
    """Convert a MapLibre style to a QGIS vector tile renderer and labeling.

    Args:
        style: The style, already limited to one archive's layers.
        sprite: The sprite image (a QImage) and its JSON index, when the style
            names a sprite.

    Returns:
        The renderer, the labeling (None when the style has no labels), and
        the converter's warnings.
    """
    converter = QgsMapBoxGlStyleConverter()
    context = QgsMapBoxGlStyleConversionContext()
    if sprite is not None:
        context.setSprites(sprite[0], sprite[1])
    converter.convert(style, context)
    warnings = [*list(context.warnings()), converter.errorMessage()]
    return converter.renderer(), converter.labeling(), [w for w in warnings if w]


def vector_tile_layer(
    server: TileServer,
    archive: Archive,
    name: str,
) -> QgsVectorTileLayer:
    """Return a vector tile layer that reads ``archive`` through ``server``."""
    uri = QgsDataSourceUri()
    uri.setParam("type", "xyz")
    uri.setParam("url", server.register(archive))
    uri.setParam("zmin", str(archive.min_zoom))
    uri.setParam("zmax", str(archive.max_zoom))
    layer = QgsVectorTileLayer(bytes(uri.encodedUri()).decode(), name)
    if not layer.isValid():
        raise LayerError(f"QGIS could not open the tiles of {archive.url}")
    layer.setCustomProperty(PMTILES_PROPERTY, archive.url)
    return layer


@dataclass
class TileStyle:
    """One catalog style, limited to the layers that read one archive."""

    name: str
    style: dict[str, Any]
    sprite: tuple[bytes, dict[str, Any]] | None


@dataclass
class TilePart:
    """One archive to add as a layer, with the catalog styles that draw it."""

    archive: Archive
    title: str
    styles: list[TileStyle]


@dataclass
class PreparedTiles:
    """PMTiles archives read and styles split, ready to become layers.

    ``prepare_pmtiles`` builds this off the main thread, because reading an
    archive header is a network request. ``build_pmtiles`` turns it into layers
    on the main thread.
    """

    parts: list[TilePart]
    warnings: list[str]


def prepare_pmtiles(
    document: Document,
    urls: list[str],
    fetch_bytes: Callable[[str], bytes],
) -> PreparedTiles:
    """Read PMTiles archives and every collection style that draws them.

    Args:
        document: The collection, for its styles, PMTiles links, and title.
        urls: The archives to open.
        fetch_bytes: Fetches the styles and their sprites.

    Raises:
        PmtilesError: An archive cannot be read.
    """
    warnings: list[str] = []
    archives = archive_urls(document)
    fallback = archives[0] if len(archives) == 1 else None
    by_archive: dict[str, list[TileStyle]] = {url: [] for url in urls}
    sprites: dict[str, tuple[bytes, dict[str, Any]] | None] = {}
    # document.styles puts the default style first, so it becomes current.
    for asset in document.styles:
        try:
            style = parse_style(fetch_bytes(asset.href))
        except (OSError, ValueError) as error:
            warnings.append(f"Could not read the style {asset.href}: {error}")
            continue
        styled = split_style(style, asset.href, fallback)
        used = [url for url in styled.by_source if url in by_archive]
        if not used:
            continue
        warnings.extend(f"{asset.label}: {note}" for note in styled.warnings)
        sprite = _sprite(style, asset.href, fetch_bytes, warnings, sprites)
        for url in used:
            by_archive[url].append(TileStyle(asset.label, styled.by_source[url], sprite))
    titles = {link.href: link.title for link in document.pmtiles}
    parts = []
    for url in urls:
        if document.styles and not by_archive[url]:
            warnings.append(f"No style in {document.title} draws {url}.")
        title = titles.get(url) or document.title
        parts.append(TilePart(open_archive(url), title, by_archive[url]))
    return PreparedTiles(parts=parts, warnings=warnings)


def build_pmtiles(
    server: TileServer,
    prepared: PreparedTiles,
    load_image: Callable[[bytes], Any],
) -> tuple[list[QgsMapLayer], list[str]]:
    """Create the styled vector tile layers. Call on the main thread."""
    warnings = list(prepared.warnings)
    layers: list[QgsMapLayer] = []
    for part in prepared.parts:
        layer = vector_tile_layer(server, part.archive, part.title)
        warnings.extend(add_styles(layer, part.styles, load_image))
        layers.append(layer)
    return layers, warnings


def add_styles(
    layer: QgsVectorTileLayer,
    styles: list[TileStyle],
    load_image: Callable[[bytes], Any],
) -> list[str]:
    """Add each catalog style to the layer as a named QGIS style.

    The first style becomes current. The layer's own style stays available
    under ``DEFAULT_STYLE_NAME``. The user switches styles from the layer's
    **Styles** menu.

    Returns:
        The converter's warnings.
    """
    warnings: list[str] = []
    original = QgsMapLayerStyle()
    original.readFromLayer(layer)
    named: dict[str, QgsMapLayerStyle] = {}
    for style in styles:
        sprite = None
        if style.sprite is not None:
            sprite = (load_image(style.sprite[0]), style.sprite[1])
        renderer, labeling, notes = convert_style(style.style, sprite)
        warnings.extend(f"{style.name}: {note}" for note in notes)
        if renderer is None:
            continue
        layer.setRenderer(renderer)
        layer.setLabeling(labeling)
        captured = QgsMapLayerStyle()
        captured.readFromLayer(layer)
        named[_unique(style.name, named)] = captured
    # Put the layer's own style back first. setCurrentStyle saves the layer's
    # state into the current style before it switches.
    original.writeToLayer(layer)
    manager = layer.styleManager()
    manager.renameStyle(manager.currentStyle(), DEFAULT_STYLE_NAME)
    for name, captured in named.items():
        manager.addStyle(name, captured)
    if named:
        manager.setCurrentStyle(next(iter(named)))
    return warnings


def _unique(name: str, taken: dict[str, Any]) -> str:
    candidate, number = name, 2
    while candidate in taken or candidate == DEFAULT_STYLE_NAME:
        candidate, number = f"{name} ({number})", number + 1
    return candidate


def _sprite(
    style: dict[str, Any],
    style_url: str,
    fetch_bytes: Callable[[str], bytes],
    warnings: list[str],
    cache: dict[str, tuple[bytes, dict[str, Any]] | None],
) -> tuple[bytes, dict[str, Any]] | None:
    base = style.get("sprite")
    if not isinstance(base, str):
        return None
    root = urljoin(style_url, base)
    if root not in cache:
        cache[root] = None
        try:
            index = json.loads(fetch_bytes(f"{root}.json"))
            image = fetch_bytes(f"{root}.png")
        except (OSError, ValueError) as error:
            warnings.append(f"Could not load the style's sprite at {root}: {error}")
        else:
            if isinstance(index, dict):
                cache[root] = (image, index)
    return cache[root]


def coordinate_columns(header: bytes) -> tuple[str, str] | None:
    """Find the longitude and latitude columns in the first line of a CSV.

    Columns named x and y count only when every complete row in ``header``
    holds a longitude and a latitude, because x and y are often projected.

    Args:
        header: The start of the file. The first line names the columns.
            The complete rows after it test x and y.

    Returns:
        The exact names of the longitude and latitude columns, or None when
        the header does not name both.
    """
    text = header.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    if not lines:
        return None
    separator = max(_CSV_SEPARATORS, key=lines[0].count)
    rows = list(csv.reader(lines, delimiter=separator))
    names = [name.strip() for name in rows[0]]
    by_folded: dict[str, str] = {}
    for name in names:
        by_folded.setdefault(name.casefold(), name)
    lon = next((by_folded[n] for n in LONGITUDE_NAMES if n in by_folded), None)
    lat = next((by_folded[n] for n in LATITUDE_NAMES if n in by_folded), None)
    if lon is None or lat is None:
        return None
    if {lon.casefold(), lat.casefold()} & _PLANAR_NAMES:
        # The last line can stop in the middle of a row.
        complete = rows[1:-1] if not text.endswith(("\n", "\r")) else rows[1:]
        if not _degrees(complete, names.index(lon), names.index(lat)):
            return None
    return lon, lat


def _degrees(rows: list[list[str]], lon: int, lat: int) -> bool:
    """Return whether ``rows`` hold at least one point and every point is in degrees."""
    seen = False
    for row in rows:
        if len(row) <= max(lon, lat):
            continue
        try:
            x, y = float(row[lon]), float(row[lat])
        except ValueError:
            continue
        if not (-180 <= x <= 180 and -90 <= y <= 90):
            return False
        seen = True
    return seen


def csv_header(url: str) -> bytes:
    """Return the first bytes of a CSV, or nothing when the server cannot send them.

    A range request keeps a large file from downloading in full. A server
    that ignores ranges gives no header, and the CSV opens as a table.
    """
    try:
        return fetch_range(url, 0, CSV_HEADER_BYTES)
    except NetworkError:
        return b""


def csv_source(url: str, columns: tuple[str, str] | None) -> str:
    """Return the OGR source that reads a CSV over HTTP.

    The QGIS delimited text provider reads local files only. Thus the CSV
    goes through the GDAL CSV driver over ``/vsicurl/``. The ``CSV:`` prefix
    selects the driver when the URL does not end in ``.csv``. The driver
    detects the separator and the column types.

    Args:
        url: The CSV.
        columns: The longitude and latitude columns, or None for a table
            with no geometry.
    """
    options = ["AUTODETECT_TYPE=YES"]
    if columns is not None:
        options += [f"X_POSSIBLE_NAMES={columns[0]}", f"Y_POSSIBLE_NAMES={columns[1]}"]
    parts = {"path": f"CSV:{gdal_path(url)}", "openOptions": options}
    return str(QgsProviderRegistry.instance().encodeUri("ogr", parts))


def csv_layer(url: str, title: str, header: Callable[[str], bytes] = csv_header) -> QgsVectorLayer:
    """Open a CSV over HTTP, as points when its header names coordinates.

    A header with longitude and latitude columns gives a point layer in
    EPSG:4326. Any other CSV opens as a table with no geometry.

    Args:
        url: The CSV.
        title: The layer name.
        header: Returns the first bytes of the file. Call off the main
            thread when the server is slow, and pass the bytes in.
    """
    columns = coordinate_columns(header(url))
    layer = QgsVectorLayer(csv_source(url, columns), title, "ogr")
    if columns is not None and layer.isValid():
        # A CSV has no CRS of its own. STAC gives coordinates as longitude
        # and latitude.
        layer.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    return layer


def asset_layer(asset: Asset, name: str | None = None) -> QgsMapLayer:
    """Open a data asset as a layer, read in place over HTTP.

    Raises:
        LayerError: The asset has no supported format, or QGIS cannot open it.
    """
    title = name or asset.label
    # QGIS draws on other threads, which keep the user's GDAL settings. A
    # file that the settings block would open here and then draw nothing.
    blocked = blocked_message(asset.href) if asset.format != "copc" else None
    if blocked is not None:
        raise LayerError(blocked)
    path = gdal_path(asset.href)
    layer: QgsMapLayer
    with remote_reads([asset.href]):
        if asset.format in {"geojson", "flatgeobuf"}:
            layer = QgsVectorLayer(path, title, "ogr")
        elif asset.format == "csv":
            layer = csv_layer(asset.href, title)
        elif asset.format == "cog":
            layer = QgsRasterLayer(path, title, "gdal")
        elif asset.format == "copc":
            layer = QgsPointCloudLayer(asset.href, title, "copc")
        else:
            raise LayerError(f"{asset.label} has no format QGIS can open in place")
    if not layer.isValid():
        summary = layer.error().summary()
        if _GENERIC in summary:
            raise LayerError(
                f"QGIS could not open {asset.href}: {explain_open_error(asset.href, summary)}"
            )
        raise LayerError(open_failure(asset.href, summary, is_flatpak()))
    return layer


def parse_style(raw: bytes) -> dict[str, Any]:
    """Parse a style file, rejecting anything but a MapLibre v8 style object."""
    style = json.loads(raw)
    if not isinstance(style, dict) or style.get("version") != 8:
        raise ValueError("Not a MapLibre GL style (version 8)")
    return style
