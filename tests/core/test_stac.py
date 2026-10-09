"""STAC document reading.

Tree, bbox, and format cases are ported from GeoLibre's
tests/stac-api.test.ts (MIT). The ANNCSU fixtures are real documents from a
registered catalog, saved on 2026-10-07.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from portolan_registry_qgis.core.stac import (
    StacError,
    asset_format,
    collection_bbox,
    horizontal_bbox,
    is_image,
    links_of,
    read_document,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "anncsu"
ANNCSU = "https://pub-1e760dc850cb4a5aa5f8afb77713f8cd.r2.dev"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_children_are_named_and_classified():
    document = read_document(
        {
            "type": "Catalog",
            "id": "warehouse",
            "links": [
                {"rel": "self", "href": "./catalog.json"},
                {"rel": "root", "href": "./catalog.json"},
                {"rel": "parent", "href": "../catalog.json"},
                {"rel": "child", "href": "./maps/collection.json", "title": "Geologic Maps"},
                {"rel": "child", "href": "./100%_coverage/catalog.json"},
                {"rel": "child", "href": "./UPPER/CATALOG.JSON"},
                {"rel": "child", "href": "./quads/"},
                {"rel": "child", "href": "/standalone.json"},
                {"rel": "child", "href": "javascript:alert(1)"},
            ],
        },
        "https://example.com/stac/catalog.json",
    )
    assert [(n.title, n.kind) for n in document.children] == [
        ("Geologic Maps", "collection"),
        ("100%_coverage", "catalog"),
        ("UPPER", "catalog"),
        ("quads", "catalog"),
        ("standalone", "catalog"),
    ]


def test_items_are_children_too():
    document = read_document(
        {
            "type": "Catalog",
            "id": "scenes",
            "links": [
                {"rel": "item", "href": "./a.json"},
                {"rel": "item", "href": "./b.json", "title": "B"},
                {"rel": "self", "href": "./scenes.json"},
            ],
        },
        "https://example.com/stac/scenes.json",
    )
    assert [(n.title, n.kind) for n in document.children] == [("stac", "item"), ("B", "item")]
    assert document.children[0].href == "https://example.com/stac/a.json"


@pytest.mark.parametrize(
    ("href", "kind"),
    [
        ("./a/collection.json?version=2", "collection"),
        ("./b/collection.json#section", "collection"),
        ("./c/COLLECTION.JSON", "collection"),
        ("./d/catalog.json", "catalog"),
    ],
)
def test_collection_links_however_written(href, kind):
    document = read_document(
        {"type": "Catalog", "id": "x", "links": [{"rel": "child", "href": href}]},
        "https://example.com/stac/catalog.json",
    )
    assert document.children[0].kind == kind


@pytest.mark.parametrize("value", [None, [1, 2], "text", {"type": "FeatureCollection"}, {}])
def test_non_stac_documents_are_refused(value):
    with pytest.raises(StacError):
        read_document(value, "https://example.com/x.json")


@pytest.mark.parametrize(
    ("extent", "bbox"),
    [
        ({"spatial": {"bbox": [[-114, 37, -109, 42]]}}, (-114, 37, -109, 42)),
        ({"spatial": {"bbox": [[-114, 37, 0, -109, 42, 2000]]}}, (-114, 37, -109, 42)),
        ({"spatial": {"bbox": []}}, None),
        ({"spatial": {"bbox": [["west", "south", "east", "north"]]}}, None),
        ({"spatial": {"bbox": [[-114, 37]]}}, None),
        ({"spatial": {"bbox": [[-114, 37, 0, -109, 42]]}}, None),
        ({"spatial": {"bbox": [[-114, 37, math.inf, 42]]}}, None),
        ({"spatial": {"bbox": [[True, 37, 0, 42]]}}, None),
        ({"temporal": {"interval": [["2024-01-01T00:00:00Z", None]]}}, None),
        ("everywhere", None),
    ],
)
def test_collection_extent(extent, bbox):
    assert collection_bbox({"extent": extent}) == bbox
    document = read_document(
        {"type": "Collection", "id": "c", "links": [], "extent": extent},
        "https://example.com/c/collection.json",
    )
    assert document.bbox == bbox


def test_item_bbox_drops_elevation():
    assert horizontal_bbox([1, 2, 10, 3, 4, 20]) == (1, 2, 3, 4)
    document = read_document(
        {"type": "Feature", "id": "i", "bbox": [1, 2, 10, 3, 4, 20], "properties": {}},
        "https://example.com/i.json",
    )
    assert document.kind == "item"
    assert document.bbox == (1, 2, 3, 4)


@pytest.mark.parametrize(
    ("href", "media", "expected"),
    [
        ("https://example.com/a.pmtiles", "application/vnd.pmtiles", "pmtiles"),
        ("https://example.com/a.PMTILES", None, "pmtiles"),
        ("https://example.com/a.pmtiles?token=1", None, "pmtiles"),
        ("https://example.com/tiles?id=7&f=pmtiles", None, None),
        ("https://example.com/a.tif", "image/tiff; application=geotiff", "cog"),
        ("https://example.com/a.TIF?download=1", None, "cog"),
        ("https://example.com/a.json", "application/geo+json", "geojson"),
        ("https://example.com/data.bin", None, None),
        ("https://example.com/data.bin", "application/vnd.apache.parquet", "parquet"),
        ("https://example.com/data.parquet", None, "parquet"),
        ("https://example.com/geotiff/a.pmtiles", None, "pmtiles"),
        # A declared media type wins over the extension.
        ("https://example.com/a.pmtiles", "application/geo+json", "geojson"),
        ("https://example.com/a.geojson", "application/vnd.pmtiles", "pmtiles"),
        ("https://example.com/a.fgb", None, "flatgeobuf"),
        ("https://example.com/a.copc.laz", "application/vnd.laszip+copc", "copc"),
        # Unresolved object-store URIs are named by no reader.
        ("abfs://us-census/2020/x.parquet", "application/x-parquet", None),
    ],
)
def test_asset_format(href, media, expected):
    assert asset_format(href, media) == expected


def test_links_of_skips_malformed_entries():
    links = links_of(
        [None, {"rel": 1, "href": "x"}, {"rel": "a"}, {"rel": "b", "href": "./b", "title": 3}],
        "https://example.com/c/",
    )
    assert [(link.rel, link.href, link.title) for link in links] == [
        ("b", "https://example.com/c/b", None)
    ]
    assert links_of("nope", "https://example.com/") == ()


def test_real_portolan_collection():
    href = f"{ANNCSU}/indirizzi/collection.json"
    document = read_document(load("indirizzi/collection.json"), href)
    assert document.kind == "collection"
    assert document.id == "indirizzi"
    assert document.license == "CC-BY-4.0"
    assert document.bbox == (6.7003259, 35.5017421, 18.660009, 47.0805336)
    (tiles,) = document.pmtiles
    assert tiles.href == f"{ANNCSU}/anncsu-indirizzi.pmtiles"
    assert tiles.layers == ("addresses",)
    style = document.default_style
    assert style is not None
    assert style.href == f"{ANNCSU}/indirizzi/styles/indirizzi.json"
    assert [a.key for a in document.data_assets] == ["data", "visual"]
    data = document.data_assets[0]
    assert data.format == "parquet"
    assert data.size == 1059267903
    assert data.checksum.startswith("1220")
    assert document.children == ()


def test_real_portolan_catalog():
    document = read_document(load("catalog.json"), f"{ANNCSU}/catalog.json")
    assert document.kind == "catalog"
    assert [n.href for n in document.children] == [
        f"{ANNCSU}/indirizzi/collection.json",
        f"{ANNCSU}/indirizzi-h3/collection.json",
        f"{ANNCSU}/rilasci/collection.json",
    ]
    assert all(n.kind == "collection" for n in document.children)


def _with_styles(*roles):
    assets = {
        f"style-{i}": {"href": f"./styles/{i}.json", "roles": list(r)} for i, r in enumerate(roles)
    }
    return read_document(
        {"type": "Collection", "id": "c", "links": [], "assets": assets},
        "https://example.com/c/collection.json",
    )


def test_default_style_selection():
    assert _with_styles(["style"], ["style", "default"]).default_style.key == "style-1"
    assert _with_styles(["style"]).default_style.key == "style-0"
    # Two styles and no default marker: nothing to pick for the user.
    assert _with_styles(["style"], ["style"]).default_style is None
    assert _with_styles().default_style is None


def test_assets_resolve_storage_options():
    document = read_document(
        {
            "type": "Feature",
            "id": "i",
            "properties": {"table:storage_options": {"account_name": "acct"}},
            "assets": {
                "a": {"href": "abfs://box/a.parquet"},
                "b": {
                    "href": "abfs://box/b.parquet",
                    "table:storage_options": {"account_name": "x"},
                },
                "bad": {"title": "no href"},
                "list": ["not", "a", "dict"],
            },
        },
        "https://example.com/i.json",
    )
    assert [(a.key, a.href, a.format) for a in document.assets] == [
        ("a", "https://acct.blob.core.windows.net/box/a.parquet", "parquet"),
        ("b", "https://x.blob.core.windows.net/box/b.parquet", "parquet"),
    ]


def test_asset_label_and_style_detection():
    document = read_document(
        {
            "type": "Collection",
            "id": "c",
            "links": [],
            "assets": {
                "s": {"href": "./s.json", "type": "application/vnd.mapbox.style+json"},
                "t": {"href": "./t.png", "title": "Preview", "roles": ["thumbnail"]},
            },
        },
        "https://example.com/c/collection.json",
    )
    style, thumb = document.assets
    assert style.is_style
    assert style.label == "s"
    assert thumb.label == "Preview"
    assert document.data_assets == ()


def test_icon_and_thumbnail():
    document = read_document(
        {
            "type": "Collection",
            "id": "c",
            "links": [
                {"rel": "icon", "href": "s3://bucket/icon.png"},
                {"rel": "icon", "href": "../icons/leaf.svg", "type": "image/svg+xml"},
            ],
            "assets": {
                "cog": {"href": "./o.tif", "type": "image/tiff", "roles": ["overview"]},
                "o": {"href": "./o.jpg", "roles": ["overview"]},
                "t": {"href": "./t.png", "type": "image/png", "roles": ["thumbnail"]},
            },
        },
        "https://example.com/c/collection.json",
    )
    assert document.icon == "https://example.com/icons/leaf.svg"
    assert document.thumbnail is not None
    assert document.thumbnail.key == "t"


def test_overview_stands_in_for_a_missing_thumbnail():
    document = read_document(
        {
            "type": "Feature",
            "id": "i",
            "links": [{"rel": "icon", "href": "./readme.txt"}],
            "assets": {"o": {"href": "./o.webp?v=2", "roles": ["overview"]}},
        },
        "https://example.com/i.json",
    )
    assert document.icon is None
    assert document.thumbnail is not None
    assert document.thumbnail.key == "o"


@pytest.mark.parametrize(
    ("href", "media", "expected"),
    [
        ("https://x/a.png", None, True),
        ("https://x/a", "image/jpeg", True),
        ("https://x/a.tif", "image/tiff; application=geotiff", False),
        ("https://x/a.json", None, False),
        ("file:///a.png", None, False),
    ],
)
def test_is_image(href, media, expected):
    assert is_image(href, media) is expected


def _with_assets(assets, links=()):
    return read_document(
        {"type": "Collection", "id": "c", "links": list(links), "assets": assets},
        "https://example.com/c/collection.json",
    )


def _asset(name, *roles):
    return {"href": f"./{name}", "roles": list(roles) or ["data"]}


def test_preferred_href_takes_the_first_pmtiles_link():
    links = [
        {"rel": "pmtiles", "href": "./main.pmtiles"},
        {"rel": "pmtiles", "href": "./other.pmtiles"},
    ]
    document = _with_assets({"data": _asset("d.parquet")}, links)
    assert document.preferred_href() == "https://example.com/c/main.pmtiles"


def test_preferred_href_falls_back_by_format():
    base = "https://example.com/c/"
    assets = {
        "upstream": _asset("u.tif", "source"),
        "geojson": _asset("d.geojson"),
        "data": _asset("d.parquet"),
        "visual": _asset("v.pmtiles", "visual"),
        "thumbnail": _asset("t.png", "thumbnail"),
    }
    assert _with_assets(assets).preferred_href() == f"{base}v.pmtiles"
    del assets["visual"]
    assert _with_assets(assets).preferred_href() == f"{base}d.parquet"
    # Without DuckDB, GeoParquet cannot load, and an upstream source never counts.
    assert _with_assets(assets).preferred_href(parquet=False) == f"{base}d.geojson"
    assert _with_assets({"upstream": assets["upstream"]}).preferred_href() is None
    assert _with_assets({"thumbnail": assets["thumbnail"]}).preferred_href() is None
