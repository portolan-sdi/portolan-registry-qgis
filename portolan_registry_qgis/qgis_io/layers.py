"""Turn STAC assets and PMTiles links into QGIS layers.

PMTiles go through the loopback tile server. Other formats open over HTTP
range requests through OGR, GDAL, or the point cloud provider.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from osgeo import gdal
from qgis.core import (
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsMapBoxGlStyleConversionContext,
    QgsMapBoxGlStyleConverter,
    QgsMapLayer,
    QgsMapLayerStyle,
    QgsPointCloudLayer,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsVectorTileLayer,
)
from qgis.PyQt.QtCore import QCoreApplication

from portolan_registry_qgis.core.coordinates import coordinate_columns
from portolan_registry_qgis.core.diagnose import open_failure
from portolan_registry_qgis.core.hrefs import (
    extension_blocked,
    plain_vsicurl_path,
    url_extension,
    vsicurl_path,
)
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.parquet_query import is_flatpak
from portolan_registry_qgis.core.style import (
    DEFAULT_STYLE_NAME,
    Sprite,
    archive_urls,
    match_archive,
    parse_style,
    read_sprite,
    split_style,
    unique_name,
)
from portolan_registry_qgis.qgis_io.crs import wgs84
from portolan_registry_qgis.qgis_io.network import NetworkError, fetch_range, http_status
from portolan_registry_qgis.qgis_io.tileserver import Archive, TileServer, open_archive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from portolan_registry_qgis.core.stac import Asset, Document

PMTILES_PROPERTY = "portolan/pmtiles_url"
# The first request for a CSV reads this many bytes, enough for the header.
CSV_HEADER_BYTES = 16384


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
    extension = url_extension(url)
    if not extension:
        return _(
            "QGIS could not open {url}. The GDAL setting {name} is {setting}, "
            "so GDAL reads no file without an extension over HTTP. Remove the setting "
            "in Settings > Options, under System > Environment or GDAL. "
            "Then restart QGIS."
        ).format(url=url, name=_ALLOWED, setting=repr(setting))
    # Translators: {name} is a GDAL setting name and {setting} its quoted value.
    # Keep "Settings > Options" and "System > Environment" as QGIS names its menus.
    return _(
        "QGIS could not open {url}. The GDAL setting {name} is {setting}, "
        "so GDAL reads no {extension} file over HTTP. Remove the setting, or add "
        "{extension} to it, in Settings > Options, under System > Environment or GDAL. "
        "Then restart QGIS."
    ).format(url=url, name=_ALLOWED, setting=repr(setting), extension=extension)


@contextlib.contextmanager
def remote_reads(urls: list[str]) -> Iterator[None]:
    """Open the files at ``urls`` with retries, and without a remembered failure.

    GDAL caches a failed request for the life of the process, so the function
    clears that cache for ``urls``. The retry settings apply to this thread only.
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
# GDAL writes it in English, so it stays English here.
_GENERIC = "does not exist in the file system"  # i18n: skip - GDAL's text, matched


def _provider_invalid() -> str:
    """Return the start of the line QGIS adds when a raster provider fails.

    QGIS translates that line, so the start comes from its own catalog.
    """
    line = QCoreApplication.translate(
        "QgsRasterLayer",
        "Provider is not valid (provider: %1, URI: %2",  # i18n: skip - QGIS's own text
    )
    return str(line).split("%1", 1)[0]


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
        return _("the server answered HTTP {status}").format(status=status)
    if status is not None:
        return _(
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
        return gdal.GetLastErrorMsg() or _("GDAL gave no reason")
    finally:
        gdal.PopErrorHandler()


def convert_style(
    style: dict[str, Any],
    sprite: tuple[Any, dict[str, Any]] | None = None,
) -> tuple[Any, Any, list[str]]:
    """Convert a MapLibre style to a QGIS vector tile renderer and labeling.

    ``style`` holds one archive's layers. ``sprite`` is a QImage and its index.
    Return the renderer, the labeling or None, and the converter's warnings.
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
        raise LayerError(_("QGIS could not open the tiles of {url}").format(url=archive.url))
    layer.setCustomProperty(PMTILES_PROPERTY, archive.url)
    apply_bounds(layer, archive)
    return layer


# The Web Mercator tile grid stops at this latitude. A bound past it
# transforms to infinity, so the extent clamps to the grid first.
_WEB_MERCATOR_LAT = 85.051129


def apply_bounds(layer: QgsVectorTileLayer, archive: Archive) -> None:
    """Give ``layer`` the extent of ``archive``, so "Zoom to Layer" frames the data.

    Call this again after ``setDataSource``, which resets the extent to the
    whole world. An archive without bounds keeps the world extent.
    """
    if archive.bounds is None:
        return
    west, south, east, north = archive.bounds
    south = max(south, -_WEB_MERCATOR_LAT)
    north = min(north, _WEB_MERCATOR_LAT)
    transform = QgsCoordinateTransform(wgs84(), layer.crs(), QgsProject.instance())
    layer.setExtent(transform.transformBoundingBox(QgsRectangle(west, south, east, north)))


@dataclass
class TileStyle:
    """One catalog style, limited to the layers that read one archive."""

    name: str
    style: dict[str, Any]
    sprite: Sprite | None


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
    """Read the PMTiles archives at ``urls`` and every style in ``document`` that draws them.

    Raises:
        PmtilesError: An archive cannot be read.
    """
    warnings: list[str] = []
    by_archive = _styles_by_archive(document, urls, fetch_bytes, warnings)
    titles = {link.href: link.title for link in document.pmtiles}
    parts = []
    for url in urls:
        if document.styles and not by_archive[url]:
            warnings.append(
                _("No style in {title} draws {url}.").format(title=document.title, url=url)
            )
        title = titles.get(url) or document.title
        parts.append(TilePart(open_archive(url), title, by_archive[url]))
    return PreparedTiles(parts=parts, warnings=warnings)


def _styles_by_archive(
    document: Document,
    urls: list[str],
    fetch_bytes: Callable[[str], bytes],
    warnings: list[str],
) -> dict[str, list[TileStyle]]:
    """Return the collection styles that draw each archive in ``urls``.

    A style that cannot be read, and each note about a style, adds a
    warning to ``warnings``.
    """
    archives = archive_urls(document)
    fallback = archives[0] if len(archives) == 1 else None
    by_archive: dict[str, list[TileStyle]] = {url: [] for url in urls}
    sprites: dict[str, Sprite | None] = {}
    # document.styles puts the default style first, so it becomes current.
    for asset in document.styles:
        try:
            style = parse_style(fetch_bytes(asset.href))
        except (OSError, ValueError) as error:
            warnings.append(
                _("Could not read the style {url}: {error}").format(url=asset.href, error=error)
            )
            continue
        styled = split_style(style, asset.href, fallback)
        used = {source: match_archive(source, urls) for source in styled.by_source}
        if not any(used.values()):
            continue
        warnings.extend(_style_notes(asset.label, styled.warnings))
        sprite = read_sprite(style, asset.href, fetch_bytes, warnings, sprites)
        for source, url in used.items():
            if url is not None:
                by_archive[url].append(TileStyle(asset.label, styled.by_source[source], sprite))
    return by_archive


def _style_notes(name: str, notes: Iterable[str]) -> list[str]:
    """Return each note about the style ``name`` as a warning that names the style."""
    return [
        # Translators: A warning about one style. {style} is the style's name.
        _("{style}: {warning}").format(style=name, warning=note)
        for note in notes
    ]


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
    """Add each catalog style to the layer as a named QGIS style, and return the warnings.

    The first style becomes current. The layer's own style stays under
    ``DEFAULT_STYLE_NAME``.
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
        warnings.extend(_style_notes(style.name, notes))
        if renderer is None:
            continue
        layer.setRenderer(renderer)
        layer.setLabeling(labeling)
        captured = QgsMapLayerStyle()
        captured.readFromLayer(layer)
        named[unique_name(style.name, named)] = captured
    # Put the layer's own style back first. setCurrentStyle saves the layer's
    # state into the current style before it switches.
    original.writeToLayer(layer)
    manager = layer.styleManager()
    manager.renameStyle(manager.currentStyle(), _(DEFAULT_STYLE_NAME))
    for name, captured in named.items():
        manager.addStyle(name, captured)
    if named:
        manager.setCurrentStyle(next(iter(named)))
    return warnings


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
    """Return the OGR source that reads the CSV at ``url`` over HTTP.

    ``columns`` names the longitude and latitude columns, or is None for a
    table. The ``CSV:`` prefix selects the driver for any URL ending.
    """
    options = ["AUTODETECT_TYPE=YES"]
    if columns is not None:
        options += [f"X_POSSIBLE_NAMES={columns[0]}", f"Y_POSSIBLE_NAMES={columns[1]}"]
    parts = {"path": f"CSV:{gdal_path(url)}", "openOptions": options}
    return str(QgsProviderRegistry.instance().encodeUri("ogr", parts))


def csv_layer(url: str, title: str, header: Callable[[str], bytes] = csv_header) -> QgsVectorLayer:
    """Open a CSV over HTTP, as EPSG:4326 points when its header names coordinates.

    ``header`` returns the first bytes of the file. Any other CSV opens as a
    table with no geometry.
    """
    columns = coordinate_columns(header(url))
    layer = QgsVectorLayer(csv_source(url, columns), title, "ogr")
    if columns is not None and layer.isValid():
        # A CSV has no CRS of its own. STAC gives coordinates as longitude
        # and latitude.
        layer.setCrs(wgs84())
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
            raise LayerError(
                _("{asset} has no format QGIS can open in place").format(asset=asset.label)
            )
    if not layer.isValid():
        summary = layer.error().summary()
        if _GENERIC in summary:
            raise LayerError(
                _("QGIS could not open {url}: {error}").format(
                    url=asset.href, error=explain_open_error(asset.href, summary)
                )
            )
        raise LayerError(
            open_failure(asset.href, summary, flatpak=is_flatpak(), invalid=_provider_invalid())
        )
    return layer
