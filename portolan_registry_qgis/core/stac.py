"""Read static STAC documents into the shapes the plugin displays.

The tree, link, bbox, and format logic is ported from GeoLibre's
``stac-api.ts`` (MIT, see NOTICE): ``linksOf``, ``catalogChildren``,
``horizontalBbox``, ``collectionBbox``, ``collectionAssetItem``, and the
``ASSET_FORMATS`` table. PMTiles links and style assets follow the Portolan
spec (``specs/portolan/formats.md``), which GeoLibre does not read.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from portolan_registry_qgis.core.hrefs import absolute_href, asset_href, folder_name, is_http_url

Bbox = tuple[float, float, float, float]
NodeKind = Literal["catalog", "collection", "item"]
AssetFormat = Literal["pmtiles", "geojson", "cog", "parquet", "flatgeobuf", "copc"]

_COLLECTION_HREF = re.compile(r"/collection\.json($|[?#])", re.IGNORECASE)
_IMAGE_HREF = re.compile(r"\.(png|jpe?g|webp|gif|svg)($|[?#])", re.IGNORECASE)
STYLE_MEDIA_TYPE = "application/vnd.mapbox.style+json"
# The order in which the panel picks an asset to add. PMTiles draw at every
# zoom with the catalog's style. A COG or COPC is the data itself. GeoParquet
# reads only the map extent, and FlatGeobuf and GeoJSON come last.
PREFERRED_FORMATS: tuple[AssetFormat, ...] = (
    "pmtiles",
    "cog",
    "copc",
    "parquet",
    "flatgeobuf",
    "geojson",
)


def is_image(href: str, media_type: str | None) -> bool:
    """Return whether an http(s) href names an image the panel can draw."""
    if not is_http_url(href):
        return False
    if media_type:
        return media_type.lower().startswith("image/") and "tiff" not in media_type.lower()
    return bool(_IMAGE_HREF.search(href))


@dataclass(frozen=True)
class Link:
    """A STAC link with its href resolved against the document it came from."""

    rel: str
    href: str
    title: str | None = None
    type: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Node:
    """A child of a catalog, before it is read.

    Only the link says what a node is, so a node classed as a catalog may turn
    out to be a collection once it is opened.
    """

    href: str
    title: str
    kind: NodeKind


@dataclass(frozen=True)
class Asset:
    """A STAC asset with its href resolved and its format detected."""

    key: str
    href: str
    title: str | None
    type: str | None
    roles: tuple[str, ...]
    size: int | None
    checksum: str | None
    format: AssetFormat | None

    @property
    def label(self) -> str:
        """The asset's title, or its key when it has none."""
        return self.title or self.key

    @property
    def is_style(self) -> bool:
        """Whether the asset is a MapLibre style file."""
        return "style" in self.roles or (self.type or "").lower().startswith(STYLE_MEDIA_TYPE)


@dataclass(frozen=True)
class PmtilesLink:
    """A ``rel: pmtiles`` link from the web-map-links extension."""

    href: str
    title: str | None
    layers: tuple[str, ...]


@dataclass(frozen=True)
class Document:
    """What a catalog, collection, or item document holds."""

    href: str
    kind: NodeKind
    id: str
    title: str
    description: str
    children: tuple[Node, ...]
    assets: tuple[Asset, ...]
    pmtiles: tuple[PmtilesLink, ...]
    bbox: Bbox | None
    license: str | None
    links: tuple[Link, ...]

    @property
    def styles(self) -> tuple[Asset, ...]:
        """The style assets, with the default style first."""
        styles = [asset for asset in self.assets if asset.is_style]
        return tuple(sorted(styles, key=lambda asset: "default" not in asset.roles))

    @property
    def default_style(self) -> Asset | None:
        """The style marked ``default``, or the only style when there is one."""
        styles = self.styles
        if not styles:
            return None
        if "default" in styles[0].roles or len(styles) == 1:
            return styles[0]
        return None

    @property
    def data_assets(self) -> tuple[Asset, ...]:
        """Assets other than styles, thumbnails, and metadata sidecars."""
        skip = {"thumbnail", "overview", "metadata"}
        return tuple(
            asset
            for asset in self.assets
            if not asset.is_style and not skip.intersection(asset.roles)
        )

    def preferred_href(self, *, parquet: bool = True) -> str | None:
        """Return the href of the asset to add when the user picks nothing else.

        The first ``rel: pmtiles`` link wins. Publishers list the archive that
        the default style reads first. Otherwise the first data asset in
        ``PREFERRED_FORMATS`` order wins. An upstream ``source`` asset never
        does.

        Args:
            parquet: Whether the plugin can read GeoParquet, which needs DuckDB.
        """
        if self.pmtiles:
            return self.pmtiles[0].href
        candidates = [a for a in self.data_assets if "source" not in a.roles]
        for wanted in PREFERRED_FORMATS:
            if wanted == "parquet" and not parquet:
                continue
            for asset in candidates:
                if asset.format == wanted:
                    return asset.href
        return None

    @property
    def icon(self) -> str | None:
        """The href of the first ``rel: icon`` link that points to an image."""
        return next(
            (
                link.href
                for link in self.links
                if link.rel == "icon" and is_image(link.href, link.type)
            ),
            None,
        )

    @property
    def thumbnail(self) -> Asset | None:
        """The first image asset with the ``thumbnail`` role, else one with ``overview``."""
        for role in ("thumbnail", "overview"):
            for asset in self.assets:
                if role in asset.roles and is_image(asset.href, asset.type):
                    return asset
        return None


class StacError(ValueError):
    """A document is not a STAC object the plugin can read."""


def links_of(value: object, base: str) -> tuple[Link, ...]:
    """Return the well-formed links in ``value`` with absolute hrefs."""
    if not isinstance(value, list):
        return ()
    links = []
    for raw in value:
        if not isinstance(raw, dict) or not isinstance(raw.get("rel"), str) or not raw.get("href"):
            continue
        try:
            href = absolute_href(str(raw["href"]), base)
        except ValueError:
            continue
        title = raw.get("title")
        media = raw.get("type")
        extra = {k: v for k, v in raw.items() if k not in {"rel", "href", "title", "type"}}
        links.append(
            Link(
                rel=raw["rel"],
                href=href,
                title=title if isinstance(title, str) else None,
                type=media if isinstance(media, str) else None,
                extra=extra,
            )
        )
    return tuple(links)


def children_of(document: dict[str, Any], base: str) -> tuple[Node, ...]:
    """Return a document's ``child`` and ``item`` links as tree nodes."""
    nodes = []
    for link in links_of(document.get("links"), base):
        if link.rel == "child" and is_http_url(link.href):
            kind: NodeKind = "collection" if _COLLECTION_HREF.search(link.href) else "catalog"
            nodes.append(Node(link.href, link.title or folder_name(link.href), kind))
        elif link.rel == "item" and is_http_url(link.href):
            nodes.append(Node(link.href, link.title or folder_name(link.href), "item"))
    return tuple(nodes)


def horizontal_bbox(values: object) -> Bbox | None:
    """Return the horizontal corners of a 2D or 3D STAC bbox."""
    if not isinstance(values, (list, tuple)) or len(values) < 4 or len(values) % 2:
        return None
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in values
    ):
        return None
    half = len(values) // 2
    return (
        float(values[0]),
        float(values[1]),
        float(values[half]),
        float(values[half + 1]),
    )


def collection_bbox(document: dict[str, Any]) -> Bbox | None:
    """Return the first spatial extent a collection declares, which covers the rest."""
    extent = document.get("extent")
    spatial = extent.get("spatial") if isinstance(extent, dict) else None
    boxes = spatial.get("bbox") if isinstance(spatial, dict) else None
    return horizontal_bbox(boxes[0]) if isinstance(boxes, list) and boxes else None


_FORMAT_RULES: tuple[tuple[AssetFormat, str, re.Pattern[str]], ...] = (
    ("pmtiles", "pmtiles", re.compile(r"\.pmtiles($|\?)", re.IGNORECASE)),
    ("geojson", "geo+json", re.compile(r"\.geojson($|\?)", re.IGNORECASE)),
    ("cog", "geotiff", re.compile(r"\.tiff?($|\?)", re.IGNORECASE)),
    ("parquet", "parquet", re.compile(r"\.(geo)?parquet($|\?)", re.IGNORECASE)),
    ("flatgeobuf", "flatgeobuf", re.compile(r"\.fgb($|\?)", re.IGNORECASE)),
    ("copc", "copc", re.compile(r"\.copc\.la[sz]($|\?)", re.IGNORECASE)),
)


def asset_format(href: str, media_type: str | None) -> AssetFormat | None:
    """Detect a format from the media type, then from the file extension.

    Returns None when the href is not http(s), because the readers behind
    "Add to map" cannot open object-store URIs that nothing resolved.
    """
    if not is_http_url(href):
        return None
    media = (media_type or "").lower()
    for name, fragment, _ in _FORMAT_RULES:
        if fragment in media:
            return name
    for name, _, extension in _FORMAT_RULES:
        if extension.search(href):
            return name
    return None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _account(raw: dict[str, Any], fallback: str | None) -> str | None:
    storage = raw.get("table:storage_options")
    if isinstance(storage, dict) and isinstance(storage.get("account_name"), str):
        return str(storage["account_name"])
    return fallback


def assets_of(document: dict[str, Any], base: str) -> tuple[Asset, ...]:
    """Return a document's assets with resolved hrefs and detected formats."""
    raw_assets = document.get("assets")
    if not isinstance(raw_assets, dict):
        return ()
    properties = document.get("properties")
    item_account = _account(properties, None) if isinstance(properties, dict) else None
    assets = []
    for key, raw in raw_assets.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("href"), str):
            continue
        try:
            href = asset_href(raw["href"], base, _account(raw, item_account))
        except ValueError:
            continue
        roles = raw.get("roles")
        title = raw.get("title")
        media = raw.get("type")
        checksum = raw.get("file:checksum")
        assets.append(
            Asset(
                key=str(key),
                href=href,
                title=title if isinstance(title, str) else None,
                type=media if isinstance(media, str) else None,
                roles=tuple(r for r in roles if isinstance(r, str))
                if isinstance(roles, list)
                else (),
                size=_int_or_none(raw.get("file:size")),
                checksum=checksum if isinstance(checksum, str) else None,
                format=asset_format(href, media if isinstance(media, str) else None),
            )
        )
    return tuple(assets)


def pmtiles_links(links: tuple[Link, ...]) -> tuple[PmtilesLink, ...]:
    """Return the ``rel: pmtiles`` links and their default-visible layers."""
    found = []
    for link in links:
        if link.rel != "pmtiles" or not is_http_url(link.href):
            continue
        layers = link.extra.get("pmtiles:layers")
        names = tuple(n for n in layers if isinstance(n, str)) if isinstance(layers, list) else ()
        found.append(PmtilesLink(link.href, link.title, names))
    return tuple(found)


def _kind_of(document: dict[str, Any]) -> NodeKind:
    kind = document.get("type")
    if kind == "Collection":
        return "collection"
    if kind == "Feature":
        return "item"
    if kind == "Catalog":
        return "catalog"
    raise StacError(f"Not a STAC catalog, collection, or item (type is {kind!r})")


def _bbox_of(document: dict[str, Any], kind: NodeKind) -> Bbox | None:
    if kind == "collection":
        return collection_bbox(document)
    return horizontal_bbox(document.get("bbox"))


def _text(document: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def read_document(document: object, href: str) -> Document:
    """Read a fetched STAC document.

    Args:
        document: The parsed JSON.
        href: The URL the document was fetched from, for resolving links.

    Returns:
        The document's children, assets, PMTiles links, and extent.

    Raises:
        StacError: The JSON is not a STAC catalog, collection, or item.
    """
    if not isinstance(document, dict):
        raise StacError("The link did not return a STAC document")
    kind = _kind_of(document)
    links = links_of(document.get("links"), href)
    properties = document.get("properties")
    props = properties if isinstance(properties, dict) else {}
    doc_id = _text(document, "id") or folder_name(href)
    license_value = document.get("license", props.get("license"))
    return Document(
        href=href,
        kind=kind,
        id=doc_id,
        title=_text(document, "title") or _text(props, "title") or doc_id,
        description=_text(document, "description") or _text(props, "description"),
        children=children_of(document, href),
        assets=assets_of(document, href),
        pmtiles=pmtiles_links(links),
        bbox=_bbox_of(document, kind),
        license=license_value if isinstance(license_value, str) else None,
        links=links,
    )
