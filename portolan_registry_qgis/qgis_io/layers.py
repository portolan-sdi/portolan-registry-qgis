"""Turn STAC assets and PMTiles links into QGIS layers.

PMTiles become native vector tile layers through the loopback tile server.
Each layer carries every collection MapLibre style that draws its archive, as
a named QGIS style. GeoJSON and FlatGeobuf open through OGR,
COGs through GDAL, and COPC through the point cloud provider, all over HTTP
range requests without a download. GeoParquet goes through DuckDB instead, in
``parquet_layer``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from qgis.core import (
    QgsDataSourceUri,
    QgsMapBoxGlStyleConversionContext,
    QgsMapBoxGlStyleConverter,
    QgsMapLayer,
    QgsMapLayerStyle,
    QgsPointCloudLayer,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsVectorTileLayer,
)

from portolan_registry_qgis.core.diagnose import open_failure
from portolan_registry_qgis.core.hrefs import vsicurl_path
from portolan_registry_qgis.core.parquet_query import is_flatpak
from portolan_registry_qgis.qgis_io.tileserver import Archive, TileServer, open_archive

if TYPE_CHECKING:
    from collections.abc import Callable

    from portolan_registry_qgis.core.stac import Asset, Document

PMTILES_PROPERTY = "portolan/pmtiles_url"
DEFAULT_STYLE_NAME = "QGIS default style"
_PMTILES_SCHEME = "pmtiles://"


class LayerError(RuntimeError):
    """QGIS could not open the asset as a layer."""


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


def asset_layer(asset: Asset, name: str | None = None) -> QgsMapLayer:
    """Open a data asset as a layer, read in place over HTTP.

    Raises:
        LayerError: The asset has no supported format, or QGIS cannot open it.
    """
    title = name or asset.label
    path = vsicurl_path(asset.href)
    layer: QgsMapLayer
    if asset.format in {"geojson", "flatgeobuf"}:
        layer = QgsVectorLayer(path, title, "ogr")
    elif asset.format == "cog":
        layer = QgsRasterLayer(path, title, "gdal")
    elif asset.format == "copc":
        layer = QgsPointCloudLayer(asset.href, title, "copc")
    else:
        raise LayerError(f"{asset.label} has no format QGIS can open in place")
    if not layer.isValid():
        raise LayerError(open_failure(asset.href, layer.error().summary(), is_flatpak()))
    return layer


def parse_style(raw: bytes) -> dict[str, Any]:
    """Parse a style file, rejecting anything but a MapLibre v8 style object."""
    style = json.loads(raw)
    if not isinstance(style, dict) or style.get("version") != 8:
        raise ValueError("Not a MapLibre GL style (version 8)")
    return style
