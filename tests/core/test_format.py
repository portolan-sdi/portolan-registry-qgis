from __future__ import annotations

import pytest

from portolan_registry_qgis.core.format import (
    catalog_summary,
    count,
    format_label,
    human_size,
    license_name,
    link_label,
)


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (None, ""),
        (-1, ""),
        (0, "0 B"),
        (999, "999 B"),
        (1000, "1.0 KB"),
        (1393694428, "1.4 GB"),
        (10**18, "1000.0 PB"),
    ],
)
def test_human_size(size, text):
    assert human_size(size) == text


@pytest.mark.parametrize(
    ("args", "label"),
    [
        (("parquet", None, "https://x/a.parquet"), "GeoParquet"),
        (("parquet", None, "https://x/a.parquet", False), "GeoParquet"),
        (("parquet", None, "https://x/a.parquet", True), "Parquet"),
        # Only Parquet has a tabular name.
        (("geojson", None, "https://x/a.geojson", True), "GeoJSON"),
        (("pmtiles", None, "https://x/a.pmtiles"), "PMTiles"),
        ((None, "application/json; charset=utf-8", "https://x/a"), "JSON"),
        ((None, "image/svg+xml", "https://x/a"), "SVG"),
        ((None, None, "https://x/readme.md?raw=1"), "MD"),
        ((None, None, "https://x/no-extension"), "File"),
        ((None, None, "https://x/a.not-alnum"), "File"),
    ],
)
def test_format_label(args, label):
    assert format_label(*args) == label


def test_count():
    assert count(1, "collection") == "1 collection"
    assert count(1200, "collection") == "1,200 collections"


@pytest.mark.parametrize(
    ("args", "text"),
    [
        ((3, 1393694428, ("CC-BY-4.0",)), "3 collections, 1.4 GB, CC-BY-4.0"),
        ((1, None, ("CC0-1.0", "ODbL-1.0")), "1 collection, CC0-1.0 and ODbL-1.0"),
        ((None, 10, ("a", "b", "c")), "10 B, 3 licenses"),
        ((None, None, ()), ""),
    ],
)
def test_catalog_summary(args, text):
    assert catalog_summary(*args) == text


@pytest.mark.parametrize(
    ("value", "name"),
    [
        (None, ""),
        ("", ""),
        ("other", "Custom terms"),
        ("Proprietary", "Proprietary"),
        ("various", "Various"),
        ("CC-BY-4.0", "CC-BY-4.0"),
    ],
)
def test_license_name(value, name):
    assert license_name(value) == name


@pytest.mark.parametrize(
    ("href", "title", "label"),
    [
        # Philadelphia's license links are both titled "License".
        (
            "https://metadata.phila.gov/#help/help-faqs/what-are-the-terms-of-use/",
            "License",
            "metadata.phila.gov",
        ),
        (
            "https://www.opendataphilly.org/datasets/city-council-districts/",
            "License",
            "opendataphilly.org",
        ),
        (
            "https://x.org/c/README.md",
            "Human-readable documentation",
            "Human-readable documentation",
        ),
        ("https://x.org/c/README.md", None, "README.md"),
        ("https://x.org/c/README.md", "Documentation", "README.md"),
        ("https://x.org/terms", "  Terms of use ", "Terms of use"),
        ("https://opendataphilly.org/datasets/land-use/", "Original source", "opendataphilly.org"),
    ],
)
def test_link_label(href, title, label):
    assert link_label(href, title) == label
