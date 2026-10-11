from __future__ import annotations

import pytest

from portolan_registry_qgis.core.format import (
    catalog_summary,
    count,
    format_label,
    human_size,
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
