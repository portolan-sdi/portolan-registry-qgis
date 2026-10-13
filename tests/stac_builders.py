"""Build small STAC documents for the tests.

Each builder returns a plain dict. It sets the fields that every document of
its type has, and takes the other fields as keyword arguments. A test passes
the fields it checks, so the reader can see them in the test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

STAC_VERSION = "1.1.0"


def links(rel: str, hrefs: Iterable[str]) -> list[dict[str, Any]]:
    """Return one link with relation ``rel`` for each href."""
    return [{"rel": rel, "href": href} for href in hrefs]


def make_catalog(
    ident: str, children: Iterable[str] = (), other_links: Iterable[dict] = (), **fields: Any
) -> dict[str, Any]:
    """Return a catalog with a child link to each href in ``children``."""
    return {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": ident,
        "links": [*other_links, *links("child", children)],
        **fields,
    }


def make_collection(
    ident: str, items: Iterable[str] = (), other_links: Iterable[dict] = (), **fields: Any
) -> dict[str, Any]:
    """Return a collection with an item link to each href in ``items``."""
    return {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "id": ident,
        "links": [*other_links, *links("item", items)],
        **fields,
    }


def make_item(
    ident: str, bbox: Sequence[float] | None = None, other_links: Iterable[dict] = (), **fields: Any
) -> dict[str, Any]:
    """Return an item. It has a ``bbox`` only when the test gives one."""
    item: dict[str, Any] = {
        "type": "Feature",
        "stac_version": STAC_VERSION,
        "id": ident,
        "properties": {},
        "links": list(other_links),
        **fields,
    }
    if bbox is not None:
        item["bbox"] = list(bbox)
    return item


def box_polygon(bbox: Sequence[float]) -> dict[str, Any]:
    """Return the GeoJSON polygon of a ``(west, south, east, north)`` box."""
    west, south, east, north = bbox
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Polygon", "coordinates": [ring]}
