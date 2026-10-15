"""Read static STAC documents into the shapes the plugin displays.

Ported from GeoLibre's ``stac-api.ts`` (MIT, see NOTICE).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from portolan_registry_qgis.core.geoparquet import GEOMETRY_NAMES
from portolan_registry_qgis.core.hrefs import (
    absolute_href,
    asset_href,
    bucket_url,
    folder_name,
    is_glob,
    is_http_url,
)
from portolan_registry_qgis.core.i18n import _

if TYPE_CHECKING:
    from collections.abc import Iterable

Bbox = tuple[float, float, float, float]
NodeKind = Literal["catalog", "collection", "item"]
AssetFormat = Literal["pmtiles", "geojson", "cog", "parquet", "flatgeobuf", "copc", "csv"]

_COLLECTION_HREF = re.compile(r"/collection\.json($|[?#])", re.IGNORECASE)
_IMAGE_HREF = re.compile(r"\.(png|jpe?g|webp|gif|svg)($|[?#])", re.IGNORECASE)
STYLE_MEDIA_TYPE = "application/vnd.mapbox.style+json"  # i18n: skip - a media type
# The order in which the panel picks an asset to add. PMTiles draw at every
# zoom with the catalog's style. A COG or COPC is the data itself. GeoParquet
# reads only the map extent, and FlatGeobuf and GeoJSON come next. A CSV comes
# last, because it often has no geometry.
PREFERRED_FORMATS: tuple[AssetFormat, ...] = (
    "pmtiles",
    "cog",
    "copc",
    "parquet",
    "flatgeobuf",
    "geojson",
    "csv",
)
# The plugin does not add assets with these roles. A ``source`` asset is the
# upstream file before conversion. A ``collection-mirror`` asset is a
# GeoParquet index of the collection's items, not the data. An ``archive``
# asset is an earlier version of the data. The browser lists all three.
_NOT_LOADABLE_ROLES = frozenset({"source", "collection-mirror", "archive"})
# Asset roles that do not hold the data.
_NOT_DATA_ROLES = frozenset({"thumbnail", "overview", "metadata"})


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
    # True for a Parquet file whose ``table:columns`` list no geometry
    # column. The spec calls this a tabular collection.
    tabular: bool = False
    # The ``partition:glob`` of a partitioned asset, resolved like an href.
    glob: str | None = None

    @property
    def read_url(self) -> str | None:
        """The URL DuckDB reads: the href, or the bucket form of the glob.

        None for a glob that only HTTPS serves, because HTTPS has no listing.
        """
        return self.href if self.glob is None else bucket_url(self.glob)

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
class Loadable:
    """A file the plugin can add to the map.

    A ``rel: pmtiles`` link has no asset. A data asset keeps its ``Asset``,
    for its size, checksum, and roles.
    """

    href: str
    title: str
    format: AssetFormat
    asset: Asset | None = None
    layers: tuple[str, ...] = ()


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
        return only_data(self.assets)

    def loadable(self) -> tuple[Loadable, ...]:
        """Return the files the plugin can add, with no duplicates.

        PMTiles links come first, then the data assets with a known format.
        """
        shared = self.pmtiles_duplicates
        found = [
            # Translators: The name of a PMTiles archive whose link has no title.
            Loadable(link.href, link.title or _("Vector tiles"), "pmtiles", layers=link.layers)
            for link in self.pmtiles
        ]
        found += [
            Loadable(asset.href, asset.label, asset.format, asset=asset)
            for asset in self.data_assets
            if asset.format is not None
            and asset.href not in shared
            and not _NOT_LOADABLE_ROLES.intersection(asset.roles)
        ]
        return tuple(found)

    @property
    def pmtiles_duplicates(self) -> frozenset[str]:
        """The hrefs that a ``rel: pmtiles`` link and an asset both name.

        A publisher can list one archive as a link and as an asset. Each
        caller keeps one of the two, so the archive shows and downloads once.
        """
        return frozenset(link.href for link in self.pmtiles) & {asset.href for asset in self.assets}

    def preferred_href(self, *, parquet: bool = True) -> str | None:
        """Return the href of the asset to add when the user picks nothing else.

        The first PMTiles link wins, else the first data asset in
        ``PREFERRED_FORMATS`` order.
        """
        if self.pmtiles:
            return self.pmtiles[0].href
        candidates = self.loadable()
        for wanted in PREFERRED_FORMATS:
            if wanted == "parquet" and not parquet:
                continue
            for candidate in candidates:
                if candidate.format == wanted:
                    return candidate.href
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


# The link keys that ``Link`` holds. ``Link.extra`` keeps the rest.
_LINK_KEYS = frozenset({"rel", "href", "title", "type"})


def int_or_none(value: object) -> int | None:
    """Return ``value`` when it is an integer, else None. A bool is not an integer."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def str_or_none(value: object, *, empty_ok: bool = True) -> str | None:
    """Return ``value`` when it is a string, else None.

    Without ``empty_ok``, an empty string returns None.
    """
    if not isinstance(value, str) or (not value and not empty_ok):
        return None
    return value


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
        links.append(
            Link(
                rel=raw["rel"],
                href=href,
                title=str_or_none(raw.get("title")),
                type=str_or_none(raw.get("type")),
                extra={k: v for k, v in raw.items() if k not in _LINK_KEYS},
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
    ("csv", "text/csv", re.compile(r"\.csv($|\?)", re.IGNORECASE)),
)


def asset_format(href: str, media_type: str | None) -> AssetFormat | None:
    """Detect a format from the media type, then from the file extension.

    Returns None when the href is not http(s), because the readers behind
    "Add to map" cannot open object-store URIs that nothing resolved.
    """
    if not is_http_url(href):
        return None
    media = (media_type or "").lower()
    for name, fragment, _pattern in _FORMAT_RULES:
        if fragment in media:
            return name
    for name, _media, extension in _FORMAT_RULES:
        if extension.search(href):
            return name
    return None


def _account(raw: dict[str, Any], fallback: str | None) -> str | None:
    storage = raw.get("table:storage_options")  # i18n: skip - a STAC key
    if isinstance(storage, dict) and isinstance(storage.get("account_name"), str):
        return str(storage["account_name"])
    return fallback


_GEOMETRY_TYPES = (
    "geometry",
    "geography",
    "wkb",
    "point",
    "linestring",
    "polygon",
    "multipoint",
    "multilinestring",
    "multipolygon",
)


def is_geometry_column(column: object) -> bool:
    """Return whether a ``table:columns`` entry describes a geometry column.

    The table extension has no geometry type. Publishers write the GeoParquet
    type, such as ``geometry``, or the storage type ``binary`` under a common
    geometry name.
    """
    if not isinstance(column, dict):
        return False
    name = str(column.get("name") or "").lower()
    kind = str(column.get("type") or "").lower()
    return name in GEOMETRY_NAMES or kind.startswith(_GEOMETRY_TYPES)


def is_tabular(columns: object) -> bool:
    """Return whether ``table:columns`` list columns and none of them is a geometry.

    A missing or empty list returns False, because it tells nothing.
    """
    if not isinstance(columns, list) or not columns:
        return False
    return not any(is_geometry_column(column) for column in columns)


def only_data(assets: Iterable[Asset]) -> tuple[Asset, ...]:
    """Return the assets other than styles, thumbnails, and metadata sidecars."""
    return tuple(
        asset
        for asset in assets
        if not asset.is_style and not _NOT_DATA_ROLES.intersection(asset.roles)
    )


def _roles(raw: object) -> tuple[str, ...]:
    """Return the string roles of an asset's ``roles`` list."""
    return tuple(r for r in raw if isinstance(r, str)) if isinstance(raw, list) else ()


def _asset(
    key: object, raw: dict[str, Any], href: str, shared_columns: object, glob: str | None
) -> Asset:
    """Read one asset whose href and glob are already resolved."""
    media = str_or_none(raw.get("type"))
    detected = asset_format(href, media)
    columns = raw.get("table:columns", shared_columns)  # i18n: skip - a STAC key
    return Asset(
        key=str(key),
        href=href,
        title=str_or_none(raw.get("title")),
        type=media,
        roles=_roles(raw.get("roles")),
        size=int_or_none(raw.get("file:size")),  # i18n: skip - a STAC key
        checksum=str_or_none(raw.get("file:checksum")),  # i18n: skip - a STAC key
        format=detected,
        tabular=detected == "parquet" and is_tabular(columns),
        glob=glob or (href if is_glob(href) else None),
    )


def _glob_of(raw: dict[str, Any], base: str) -> str | None:
    """Return the resolved ``partition:glob`` of a collection or an asset, or None."""
    glob = raw.get("partition:glob")  # i18n: skip - a STAC key
    if not isinstance(glob, str) or not glob:
        return None
    try:
        return asset_href(glob, base)
    except ValueError:
        return None


def with_partitions(
    document: dict[str, Any], base: str, assets: tuple[Asset, ...]
) -> tuple[Asset, ...]:
    """Apply a collection's ``partition:glob`` to its assets.

    A glob asset that DuckDB cannot list takes the collection glob. A
    collection with no Parquet data gets one asset that reads the glob.
    """
    glob = _glob_of(document, base)
    if glob is None:
        return assets
    fixed = tuple(
        replace(asset, glob=glob) if asset.glob and asset.read_url is None else asset
        for asset in assets
    )
    if any(asset.format == "parquet" for asset in only_data(fixed)):
        return fixed
    shared = document.get("table:columns")  # i18n: skip - a STAC key
    partitions = Asset(
        key="partition:glob",  # i18n: skip - a STAC key
        href=glob,
        title=_("All partitions"),
        type="application/vnd.apache.parquet",  # i18n: skip - a media type
        roles=("data",),
        size=None,
        checksum=None,
        format="parquet",
        tabular=is_tabular(shared),
        glob=glob,
    )
    return (*fixed, partitions)


def assets_of(document: dict[str, Any], base: str) -> tuple[Asset, ...]:
    """Return a document's assets with resolved hrefs and detected formats.

    A Parquet asset is tabular when its own ``table:columns``, else the
    document's or the item properties', list no geometry column.
    """
    raw_assets = document.get("assets")
    if not isinstance(raw_assets, dict):
        return ()
    properties = document.get("properties")
    props = properties if isinstance(properties, dict) else {}
    item_account = _account(props, None)
    shared_columns = document.get("table:columns")  # i18n: skip - a STAC key
    if shared_columns is None:
        shared_columns = props.get("table:columns")  # i18n: skip - a STAC key
    assets = []
    for key, raw in raw_assets.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("href"), str):
            continue
        try:
            href = asset_href(raw["href"], base, _account(raw, item_account))
        except ValueError:
            continue
        assets.append(_asset(key, raw, href, shared_columns, _glob_of(raw, base)))
    return tuple(assets)


def pmtiles_links(links: tuple[Link, ...]) -> tuple[PmtilesLink, ...]:
    """Return the ``rel: pmtiles`` links and their default-visible layers."""
    found = []
    for link in links:
        if link.rel != "pmtiles" or not is_http_url(link.href):
            continue
        layers = link.extra.get("pmtiles:layers")  # i18n: skip - a STAC key
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
    raise StacError(
        _("Not a STAC catalog, collection, or item (type is {kind})").format(kind=repr(kind))
    )


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


@dataclass(frozen=True)
class Fetched:
    """A fetched JSON document, and the URL it came from after any redirect."""

    document: object
    url: str


def read_fetched(fetched: object, href: str) -> Document:
    """Read a ``Fetched`` document, or plain JSON, that the plugin asked for at ``href``."""
    if isinstance(fetched, Fetched):
        return read_document(fetched.document, href, fetched.url)
    return read_document(fetched, href)


def read_document(document: object, href: str, base: str | None = None) -> Document:
    """Read a fetched STAC document. Relative hrefs resolve against ``base``, else ``href``.

    Raises:
        StacError: The JSON is not a STAC catalog, collection, or item.
    """
    if not isinstance(document, dict):
        raise StacError(_("The link did not return a STAC document"))
    kind = _kind_of(document)
    href_base = base or href
    links = links_of(document.get("links"), href_base)
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
        children=children_of(document, href_base),
        assets=with_partitions(document, href_base, assets_of(document, href_base)),
        pmtiles=pmtiles_links(links),
        bbox=_bbox_of(document, kind),
        license=license_value if isinstance(license_value, str) else None,
        links=links,
    )
