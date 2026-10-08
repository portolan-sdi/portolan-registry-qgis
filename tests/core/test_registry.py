"""Registry parsing.

The discovery cases are ported from GeoLibre's tests/portolan-registry.test.ts
(MIT). The ``portolan_registry:*`` cases cover the fields GeoLibre drops.
"""

from __future__ import annotations

import pytest
from portolan import RegistryCatalogEntry

from portolan_registry_qgis.core import registry as registry_module
from portolan_registry_qgis.core.registry import (
    REGISTRY_URL,
    RegistryError,
    filter_entries,
    parse_registry,
    statuses,
)

ANNCSU = {
    "rel": "child",
    "href": "https://pub-1e760dc850cb4a5aa5f8afb77713f8cd.r2.dev/catalog.json",
    "type": "application/json",
    "title": "Indirizzi ANNCSU",
    "bbox": [6.7003259, 35.5017421, 18.660009, 47.0805336],
    "portolan_registry:id": "anncsu",
    "portolan_registry:status": "valid",
    "portolan_registry:logo": None,
    "portolan_registry:updated": "2026-09-19T15:07:48Z",
    "portolan_registry:licenses": {"CC-BY-4.0": 3},
    "portolan_registry:collection_count": 3,
    "portolan_registry:feature_count": 41462130,
    "portolan_registry:total_size_bytes": 1393694428,
    "portolan_registry:failure_reason": None,
}
BOLOGNA = {
    "rel": "child",
    "href": "https://storage.googleapis.com/carto-open-catalogs/bologna-open-data/main/catalog.json",
    "title": "Open Data Comune di Bologna",
    "bbox": [11.22, 44.42, 11.43, 44.56],
    "portolan_registry:id": "bologna-open-data",
    "portolan_registry:status": "stale",
    "portolan_registry:logo": {
        "href": "https://storage.googleapis.com/carto-open-catalogs/bologna-open-data/main/_assets/bologna-open-data.png",
        "type": "image/png",
    },
    "portolan_registry:licenses": {"CC-BY-4.0": 10, "ODbL-1.0": 2},
    "portolan_registry:collection_count": 12,
    "portolan_registry:failure_reason": "HTTP 503",
}


def registry(*links):
    return {"type": "Catalog", "links": [{"rel": "self", "href": REGISTRY_URL}, *links]}


def test_discovery_reads_only_catalog_links_and_resolves_relative_urls():
    entries = parse_registry(
        registry(
            {
                "rel": "child",
                "href": "https://utrecht.blob.core.windows.net/catalog/catalog.json",
                "title": "Utrecht",
                "portolan_registry:id": "utrecht",
            },
            {
                "rel": "child",
                "href": "./example/catalog.json",
                "title": "Example",
                "portolan_registry:id": "example",
            },
            {
                "rel": "child",
                "href": "javascript:alert(1)",
                "title": "Invalid",
                "portolan_registry:id": "unsafe-scheme",
                "portolan_registry:status": "valid",
            },
            {
                "rel": "child",
                "href": "https://example.org/catalog.json",
                "portolan_registry:id": "example-org",
            },
            {"rel": "item", "href": "./item.json", "title": "Not a catalog"},
            None,
        )
    )
    assert len(entries) == 3
    assert entries[0].title == "Example"
    assert entries[0].url == (
        "https://raw.githubusercontent.com/portolan-sdi/portolan-registry/"
        "refs/heads/main/exports/example/catalog.json"
    )
    assert any(entry.title == "https://example.org/catalog.json" for entry in entries)


@pytest.mark.parametrize("value", [None, [], {}, {"type": "Catalog", "links": None}])
def test_invalid_registry_documents_raise(value):
    with pytest.raises(RegistryError, match="invalid catalog list"):
        parse_registry(value)


def test_empty_registry_is_empty():
    assert parse_registry({"type": "Catalog", "links": []}) == []


def test_registry_fields_are_kept():
    anncsu, bologna = parse_registry(registry(ANNCSU, BOLOGNA))
    assert anncsu.id == "anncsu"
    assert anncsu.status == "valid"
    assert anncsu.bbox == (6.7003259, 35.5017421, 18.660009, 47.0805336)
    assert anncsu.licenses == ("CC-BY-4.0",)
    assert anncsu.collection_count == 3
    assert anncsu.feature_count == 41462130
    assert anncsu.total_size_bytes == 1393694428
    assert anncsu.updated == "2026-09-19T15:07:48Z"
    assert anncsu.logo_url is None
    assert bologna.logo_url.endswith("bologna-open-data.png")
    assert bologna.licenses == ("CC-BY-4.0", "ODbL-1.0")
    assert bologna.failure_reason == "HTTP 503"
    assert bologna.total_size_bytes is None


def test_missing_fields_fall_back():
    (entry,) = parse_registry(
        registry(
            {
                "rel": "child",
                "href": "https://x.test/catalog.json",
                "portolan_registry:id": "x",
            }
        )
    )
    assert entry.id == "x"
    assert entry.status == "unknown"
    assert entry.bbox is None
    assert entry.licenses == ()


def test_wrongly_typed_counts_are_dropped():
    link = {
        **ANNCSU,
        "portolan_registry:collection_count": True,
        "portolan_registry:feature_count": "many",
    }
    (entry,) = parse_registry(registry(link))
    assert entry.collection_count is None
    assert entry.feature_count is None


def test_filters():
    entries = parse_registry(registry(ANNCSU, BOLOGNA))
    assert [e.id for e in filter_entries(entries, "bolo")] == ["bologna-open-data"]
    assert [e.id for e in filter_entries(entries, "odbl")] == ["bologna-open-data"]
    assert [e.id for e in filter_entries(entries, "  ")] == ["anncsu", "bologna-open-data"]
    assert [e.id for e in filter_entries(entries, status="valid")] == ["anncsu"]
    # Sicily intersects ANNCSU's Italy-wide extent, not Bologna's.
    sicily = (12.4, 36.6, 15.7, 38.3)
    assert [e.id for e in filter_entries(entries, bbox=sicily)] == ["anncsu"]
    assert statuses(entries) == ["stale", "valid"]


def test_extent_filter_keeps_catalogs_without_extent():
    (entry,) = parse_registry(
        registry(
            {
                "rel": "child",
                "href": "https://x.test/catalog.json",
                "portolan_registry:id": "x",
            }
        )
    )
    assert entry.intersects((0, 0, 1, 1))


def test_registry_discovery_delegates_to_portolan_python(monkeypatch):
    source = registry(ANNCSU)
    calls = []

    def load_registry_entries(url, *, fetch_json, include_stale):
        calls.append((url, fetch_json(url), include_stale))
        return [
            RegistryCatalogEntry(
                id="anncsu",
                url=ANNCSU["href"],
                title="Indirizzi ANNCSU",
                status="valid",
                bbox=(6.7003259, 35.5017421, 18.660009, 47.0805336),
            )
        ]

    monkeypatch.setattr(registry_module, "load_registry_entries", load_registry_entries)

    entries = registry_module.parse_registry(source, REGISTRY_URL)

    assert calls == [(REGISTRY_URL, source, True)]
    assert entries[0].id == "anncsu"
    assert entries[0].bbox == (6.7003259, 35.5017421, 18.660009, 47.0805336)
