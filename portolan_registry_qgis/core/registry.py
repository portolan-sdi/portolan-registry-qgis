"""Read the Portolan registry export into catalog entries.

The registry publishes ``exports/catalogs.json`` as a static STAC catalog whose
``child`` links are the registered catalogs. Ported from GeoLibre's
``portolanIndexFromDocument`` (MIT, see NOTICE), extended to keep the
``portolan_registry:*`` fields that GeoLibre drops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from portolan_registry_qgis.core.hrefs import is_http_url
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.stac import (
    Bbox,
    horizontal_bbox,
    int_or_none,
    links_of,
    str_or_none,
)

REGISTRY_URL = (
    "https://raw.githubusercontent.com/portolan-sdi/portolan-registry/"  # i18n: skip - a URL
    "refs/heads/main/exports/catalogs.json"
)
_PREFIX = "portolan_registry:"


class RegistryError(ValueError):
    """The registry returned something other than a catalog list."""


@dataclass(frozen=True)
class CatalogEntry:
    """One registered catalog and the facts the registry recorded about it."""

    id: str
    title: str
    url: str
    status: str
    bbox: Bbox | None
    licenses: tuple[str, ...]
    collection_count: int | None
    feature_count: int | None
    total_size_bytes: int | None
    updated: str | None
    logo_url: str | None
    failure_reason: str | None

    def matches(self, text: str) -> bool:
        """Return whether ``text`` appears in the title, id, URL, or licenses."""
        needle = text.strip().casefold()
        if not needle:
            return True
        haystack = " ".join((self.title, self.id, self.url, *self.licenses)).casefold()
        return needle in haystack

    def intersects(self, bbox: Bbox) -> bool:
        """Return whether the catalog's extent overlaps ``bbox``.

        A catalog without a recorded extent is kept, because the filter cannot
        rule it out.
        """
        if self.bbox is None:
            return True
        west, south, east, north = self.bbox
        return west <= bbox[2] and east >= bbox[0] and south <= bbox[3] and north >= bbox[1]


def _entry(link: dict[str, Any], href: str, title: str | None) -> CatalogEntry:
    def field(name: str) -> object:
        return link.get(_PREFIX + name)

    licenses = field("licenses")
    logo = field("logo")
    return CatalogEntry(
        id=str_or_none(field("id"), empty_ok=False) or href,
        title=title or href,
        url=href,
        status=str_or_none(field("status"), empty_ok=False) or "unknown",
        bbox=horizontal_bbox(link.get("bbox")),
        licenses=tuple(sorted(licenses)) if isinstance(licenses, dict) else (),
        collection_count=int_or_none(field("collection_count")),
        feature_count=int_or_none(field("feature_count")),
        total_size_bytes=int_or_none(field("total_size_bytes")),
        updated=str_or_none(field("updated"), empty_ok=False),
        logo_url=str_or_none(logo.get("href"), empty_ok=False) if isinstance(logo, dict) else None,
        failure_reason=str_or_none(field("failure_reason"), empty_ok=False),
    )


def parse_registry(document: object, base: str = REGISTRY_URL) -> list[CatalogEntry]:
    """Return the registry's catalogs, sorted by title.

    Args:
        document: The parsed ``catalogs.json``.
        base: The URL it was fetched from, for resolving relative links.

    Raises:
        RegistryError: The document is not a STAC catalog with a links list.
    """
    if not isinstance(document, dict) or document.get("type") != "Catalog":
        raise RegistryError(_("The Portolan registry returned an invalid catalog list"))
    raw_links = document.get("links")
    if not isinstance(raw_links, list):
        raise RegistryError(_("The Portolan registry returned an invalid catalog list"))
    entries: list[CatalogEntry] = []
    for raw in raw_links:
        # One link at a time, so a link links_of drops cannot shift the
        # pairing between a raw link and its resolved href.
        entries.extend(
            _entry(raw, link.href, link.title)
            for link in links_of([raw], base)
            if link.rel == "child" and is_http_url(link.href)
        )
    return sorted(entries, key=lambda entry: entry.title.casefold())


def statuses(entries: list[CatalogEntry]) -> list[str]:
    """Return the distinct statuses in ``entries``, sorted."""
    return sorted({entry.status for entry in entries})


def filter_entries(
    entries: list[CatalogEntry],
    text: str = "",
    status: str | None = None,
    bbox: Bbox | None = None,
) -> list[CatalogEntry]:
    """Return the entries that match every filter given."""
    return [
        entry
        for entry in entries
        if entry.matches(text)
        and (status is None or entry.status == status)
        and (bbox is None or entry.intersects(bbox))
    ]
