"""Adapt portolan-python registry entries for the QGIS panel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from portolan import DEFAULT_REGISTRY_URL, RegistryCatalogEntry, load_registry_entries

if TYPE_CHECKING:
    from portolan_registry_qgis.core.stac import Bbox

REGISTRY_URL = DEFAULT_REGISTRY_URL


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


def _entry(entry: RegistryCatalogEntry) -> CatalogEntry:
    return CatalogEntry(
        id=entry.id,
        title=entry.title or entry.url,
        url=entry.url,
        status=entry.status or "unknown",
        bbox=entry.bbox,
        licenses=entry.licenses,
        collection_count=entry.collection_count,
        feature_count=entry.feature_count,
        total_size_bytes=entry.total_size_bytes,
        updated=entry.updated,
        logo_url=entry.logo_url,
        failure_reason=entry.failure_reason,
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
        raise RegistryError("The Portolan registry returned an invalid catalog list")
    raw_links = document.get("links")
    if not isinstance(raw_links, list):
        raise RegistryError("The Portolan registry returned an invalid catalog list")

    def fetched_registry(_url: str) -> dict[str, Any]:
        return document

    entries = [
        _entry(entry)
        for entry in load_registry_entries(
            base,
            fetch_json=fetched_registry,
            include_stale=True,
        )
    ]
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
