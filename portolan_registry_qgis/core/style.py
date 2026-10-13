"""Read the MapLibre styles of a collection and split them per PMTiles archive.

QGIS draws one archive per vector tile layer, so each archive gets the
style layers that read from it. ``qgis_io.layers`` converts each part with
the QGIS MapBox GL converter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from portolan_registry_qgis.core.hrefs import absolute_href
from portolan_registry_qgis.core.i18n import N_, _

if TYPE_CHECKING:
    from collections.abc import Callable

    from portolan_registry_qgis.core.stac import Document

# The name the layer's own style gets in its style manager. Translate it where it is used.
DEFAULT_STYLE_NAME = N_("QGIS default style")
_PMTILES_SCHEME = "pmtiles://"
# A sprite image as bytes, and its JSON index.
Sprite = tuple[bytes, dict[str, Any]]


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


def _archive_url(source: object, style_url: str, fallback: str | None) -> str | None:
    """Return the absolute archive URL of one style source, or None for another source."""
    if not isinstance(source, dict) or source.get("type") != "vector":
        return None
    url = source.get("url")
    if url is None and "tiles" not in source:
        return fallback
    if not isinstance(url, str):
        return None
    if url.startswith(_PMTILES_SCHEME):
        url = url[len(_PMTILES_SCHEME) :]
    elif not urlsplit(url).path.lower().endswith(".pmtiles"):
        return None
    return absolute_href(url, style_url)


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
    if not isinstance(sources, dict):
        return {}
    found = {
        str(source_id): _archive_url(source, style_url, fallback)
        for source_id, source in sources.items()
    }
    return {source_id: url for source_id, url in found.items() if url is not None}


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
                _(
                    "Style layer {layer} reads source {source}, "
                    "which is not a PMTiles archive; skipped."
                ).format(layer=repr(layer.get("id")), source=repr(source))
            )
            continue
        target = result.by_source.setdefault(url, {**style, "layers": []})
        target["layers"].append(layer)
    return result


def parse_style(raw: bytes) -> dict[str, Any]:
    """Parse a style file, rejecting anything but a MapLibre v8 style object."""
    style = json.loads(raw)
    if not isinstance(style, dict) or style.get("version") != 8:
        raise ValueError(_("Not a MapLibre GL style (version 8)"))
    return style


def sprite_root(style: dict[str, Any], style_url: str) -> str | None:
    """Return the sprite URL without its ``.json`` or ``.png`` suffix, or None.

    The style names the sprite relative to the style file.
    """
    base = style.get("sprite")
    if not isinstance(base, str):
        return None
    return absolute_href(base, style_url)


def read_sprite(
    style: dict[str, Any],
    style_url: str,
    fetch_bytes: Callable[[str], bytes],
    warnings: list[str],
    cache: dict[str, Sprite | None],
) -> Sprite | None:
    """Fetch the sprite that a style names, once per sprite URL.

    Args:
        style: The style.
        style_url: The URL of the style file.
        fetch_bytes: Fetches the sprite index and image.
        warnings: Gets a warning when the sprite cannot be read.
        cache: The sprites read so far, by URL. A sprite that cannot be
            read is None.

    Returns:
        The sprite image and its index, or None when the style names no
        sprite or the sprite cannot be read.
    """
    root = sprite_root(style, style_url)
    if root is None:
        return None
    if root not in cache:
        cache[root] = None
        try:
            index = json.loads(fetch_bytes(f"{root}.json"))
            image = fetch_bytes(f"{root}.png")
        except (OSError, ValueError) as error:
            warnings.append(
                _("Could not load the style's sprite at {url}: {error}").format(
                    url=root, error=error
                )
            )
        else:
            if isinstance(index, dict):
                cache[root] = (image, index)
    return cache[root]


def unique_name(name: str, taken: dict[str, Any]) -> str:
    """Return ``name``, with a number when ``taken`` or the default style has it."""
    candidate, number = name, 2
    default = _(DEFAULT_STYLE_NAME)
    while candidate in taken or candidate == default:
        candidate, number = f"{name} ({number})", number + 1
    return candidate
