from __future__ import annotations

import json

import pytest

from portolan_registry_qgis.core.stac import read_document
from portolan_registry_qgis.core.style import (
    DEFAULT_STYLE_NAME,
    archive_urls,
    match_archive,
    parse_style,
    pmtiles_sources,
    read_sprite,
    split_style,
    sprite_root,
    unique_name,
)

STYLE_URL = "https://x.test/c/styles/s.json"
_S3_WEBSITE = "http://bucket.s3-website-us-east-1.amazonaws.com"
_S3_REST = "https://bucket.s3.us-east-1.amazonaws.com"


def test_style_split_and_sources():
    style = {
        "version": 8,
        "sources": {
            "a": {"type": "vector", "url": "pmtiles://../a.pmtiles"},
            "b": {"type": "vector", "url": "pmtiles://https://x.test/b.pmtiles"},
            "bare": {"type": "vector", "url": "../d.pmtiles?v=2"},
            "tilejson": {"type": "vector", "url": "https://tiles.test/v.json"},
            "xyz": {"type": "vector", "tiles": ["https://tiles.test/{z}/{x}/{y}.pbf"]},
            "base": {"type": "raster", "tiles": ["https://tiles.test/{z}/{x}/{y}.png"]},
            "odd": {"type": "vector", "url": 3},
            "broken": "not a source",
        },
        "layers": [
            {"id": "bg", "type": "background"},
            {"id": "la", "type": "fill", "source": "a", "source-layer": "l"},
            {"id": "lb", "type": "line", "source": "b", "source-layer": "l"},
            {"id": "sat", "type": "raster", "source": "base"},
            {"id": "none", "type": "fill"},
            "not a layer",
        ],
    }
    assert pmtiles_sources(style, STYLE_URL) == {
        "a": "https://x.test/c/a.pmtiles",
        "b": "https://x.test/b.pmtiles",
        "bare": "https://x.test/c/d.pmtiles?v=2",
    }
    split = split_style(style, STYLE_URL)
    assert {k: [layer["id"] for layer in v["layers"]] for k, v in split.by_source.items()} == {
        "https://x.test/c/a.pmtiles": ["bg", "la"],
        "https://x.test/b.pmtiles": ["lb"],
    }
    assert len(split.warnings) == 2
    assert "sat" in split.warnings[0]
    assert "none" in split.warnings[1]


def test_a_style_without_sources_or_layers():
    assert pmtiles_sources({"version": 8}, STYLE_URL) == {}
    split = split_style({"version": 8, "layers": "x"}, STYLE_URL)
    assert split.by_source == {}
    assert split.warnings == []


def test_background_without_an_archive_is_skipped():
    split = split_style({"version": 8, "layers": [{"id": "bg", "type": "background"}]}, STYLE_URL)
    assert split.by_source == {}
    assert "bg" in split.warnings[0]


def test_source_without_url_reads_the_only_archive():
    style = {"version": 8, "sources": {"data": {"type": "vector"}}, "layers": []}
    assert pmtiles_sources(style, STYLE_URL) == {}
    assert pmtiles_sources(style, STYLE_URL, "https://x.test/c/a.pmtiles") == {
        "data": "https://x.test/c/a.pmtiles"
    }


def test_a_pmtiles_tile_template_names_its_archive():
    template = "pmtiles://https://cdn.test/base.pmtiles/{z}/{x}/{y}"
    style = {
        "version": 8,
        "sources": {
            "overture": {"type": "vector", "tiles": [template]},
            "relative": {"type": "vector", "tiles": ["pmtiles://../a.pmtiles/{z}/{x}/{y}.mvt"]},
            "xyz": {"type": "vector", "tiles": ["https://tiles.test/{z}/{x}/{y}.pbf"]},
        },
        "layers": [],
    }
    assert pmtiles_sources(style, STYLE_URL, "https://x.test/c/only.pmtiles") == {
        "overture": "https://cdn.test/base.pmtiles",
        "relative": "https://x.test/c/a.pmtiles",
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.test/c/a.pmtiles", "https://x.test/c/a.pmtiles"),
        ("https://cdn.test/mirror/a.pmtiles", "https://x.test/c/a.pmtiles"),
        ("https://x.test/c/z.pmtiles", None),
        ("https://x.test/other/b.pmtiles", None),
    ],
)
def test_match_archive_by_url_then_unique_file_name(url, expected):
    archives = [
        "https://x.test/c/a.pmtiles",
        "https://x.test/c/b.pmtiles",
        "https://x.test/d/b.pmtiles",
    ]
    assert match_archive(url, archives) == expected


def test_style_urls_upgrade_s3_website_endpoints():
    style = {
        "version": 8,
        "sprite": f"{_S3_WEBSITE}/c/sprites/s",
        "sources": {"a": {"type": "vector", "url": f"pmtiles://{_S3_WEBSITE}/c/a.pmtiles"}},
        "layers": [],
    }
    url = f"{_S3_REST}/c/styles/s.json"
    assert pmtiles_sources(style, url) == {"a": f"{_S3_REST}/c/a.pmtiles"}
    fetched: list[str] = []

    def fetch(target: str) -> bytes:
        fetched.append(target)
        raise OSError("offline")

    warnings: list[str] = []
    assert read_sprite(style, url, fetch, warnings, {}) is None
    assert fetched == [f"{_S3_REST}/c/sprites/s.json"]
    assert "offline" in warnings[0]


def test_read_sprite_reads_each_sprite_once():
    style = {"version": 8, "sprite": "../sprites/s"}
    files = {
        "https://x.test/c/sprites/s.json": json.dumps({"dot": {"x": 0}}).encode(),
        "https://x.test/c/sprites/s.png": b"png",
    }
    fetched: list[str] = []

    def fetch(target: str) -> bytes:
        fetched.append(target)
        return files[target]

    cache: dict = {}
    sprite = read_sprite(style, STYLE_URL, fetch, [], cache)
    assert sprite == (b"png", {"dot": {"x": 0}})
    assert read_sprite(style, STYLE_URL, fetch, [], cache) is sprite
    assert len(fetched) == 2


def test_read_sprite_without_a_sprite_or_with_a_bad_index():
    assert sprite_root({"version": 8}, STYLE_URL) is None
    assert read_sprite({"version": 8}, STYLE_URL, lambda _url: b"", [], {}) is None
    warnings: list[str] = []
    sprite = read_sprite({"sprite": "s"}, STYLE_URL, lambda _url: b"[]", warnings, {})
    assert sprite is None
    assert warnings == []


def test_parse_style_rejects_non_styles():
    assert parse_style(b'{"version": 8, "layers": []}')["version"] == 8
    for raw in (b"[]", b'{"version": 7}'):
        with pytest.raises(ValueError, match="MapLibre"):
            parse_style(raw)


def test_unique_name():
    assert unique_name("red", {}) == "red"
    assert unique_name("red", {"red": 1, "red (2)": 1}) == "red (3)"
    assert unique_name(DEFAULT_STYLE_NAME, {}) == f"{DEFAULT_STYLE_NAME} (2)"


def test_archive_urls_come_from_links_then_assets():
    href = "https://x.test/c/collection.json"
    document = read_document(
        {
            "type": "Collection",
            "id": "c",
            "links": [{"rel": "pmtiles", "href": "./a.pmtiles", "type": "application/vnd.pmtiles"}],
            "assets": {
                "a": {"href": "./a.pmtiles", "type": "application/vnd.pmtiles"},
                "b": {"href": "./b.pmtiles", "type": "application/vnd.pmtiles"},
                "c": {"href": "./c.parquet", "type": "application/vnd.apache.parquet"},
            },
        },
        href,
    )
    assert archive_urls(document) == ["https://x.test/c/a.pmtiles", "https://x.test/c/b.pmtiles"]
