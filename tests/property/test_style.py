"""Property tests for splitting MapLibre styles per PMTiles archive."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import style
from tests.property import strategies

if TYPE_CHECKING:
    import random

archives = st.builds("{}.pmtiles".format, strategies.paths(1))
sources = st.one_of(
    st.builds(
        lambda url: {"type": "vector", "url": url},
        st.one_of(archives, archives.map("pmtiles://{}".format)),
    ),
    st.builds(
        lambda url: {"type": "vector", "tiles": [f"pmtiles://{url}/{{z}}/{{x}}/{{y}}.mvt"]},
        archives,
    ),
    st.just({"type": "vector"}),
    st.builds(lambda url: {"type": "raster", "url": url}, archives),
    st.builds(lambda url: {"type": "vector", "url": url}, strategies.files),
)


@st.composite
def styles(draw: st.DrawFn) -> dict[str, object]:
    """Return a MapLibre style whose layers read its sources, an unknown source, or none."""
    named = draw(st.dictionaries(st.text(max_size=4), sources, max_size=3))
    ids = st.sampled_from([*named, "missing"])
    layer = st.builds(
        lambda kind, source, ident: {"id": ident, "type": kind, "source": source},
        st.sampled_from(("fill", "line", "background")),
        ids,
        st.text(max_size=4),
    )
    junk = st.one_of(st.none(), st.integers(), st.text(max_size=4))
    layers = draw(st.lists(st.one_of(layer, junk), max_size=6))
    return {"version": 8, "name": draw(st.text(max_size=6)), "sources": named, "layers": layers}


@given(
    styles(),
    strategies.folders,
    st.one_of(st.none(), strategies.folders.map("{}data.pmtiles".format)),
)
def test_split_style_places_each_layer_once_and_in_order(
    raw: dict[str, object], folder: str, fallback: str | None
) -> None:
    styled = style.split_style(raw, folder + "styles/a.json", fallback)
    raw_layers = raw["layers"]
    assert isinstance(raw_layers, list)
    layers = [layer for layer in raw_layers if isinstance(layer, dict)]
    placed = [layer for part in styled.by_source.values() for layer in part["layers"]]
    assert len(placed) + len(styled.warnings) == len(layers)
    assert set(styled.by_source) <= set(
        style.pmtiles_sources(raw, folder + "styles/a.json", fallback).values()
    )
    for part in styled.by_source.values():
        assert [layer for layer in layers if any(layer is kept for kept in part["layers"])] == part[
            "layers"
        ]
        assert {key: value for key, value in part.items() if key != "layers"} == {
            key: value for key, value in raw.items() if key != "layers"
        }


@given(strategies.folders, archives)
def test_every_spelling_of_an_archive_resolves_to_the_same_url(folder: str, name: str) -> None:
    # formats.md, Vector: a style sets "`sources.data.url` as the relative
    # path from `styles/` to the PMTiles file (typically `../filename.pmtiles`)".
    relative = f"../{name}"
    spellings = (
        {"type": "vector", "url": relative},
        {"type": "vector", "url": f"pmtiles://{relative}"},
        {"type": "vector", "tiles": [f"pmtiles://{relative}/{{z}}/{{x}}/{{y}}.pbf"]},
    )
    found = {
        style.pmtiles_sources({"sources": {"data": source}}, folder + "styles/style.json")["data"]
        for source in spellings
    }
    assert found == {folder + name}


@given(st.lists(strategies.folders.map("{}a.pmtiles".format), unique=True), archives, st.randoms())
def test_match_archive_does_not_depend_on_the_archive_order(
    archives_found: list[str], name: str, rnd: random.Random
) -> None:
    url = "https://x.test/" + name
    match = style.match_archive(url, archives_found)
    assert style.match_archive(url, rnd.sample(archives_found, len(archives_found))) == match
    assert style.match_archive(url, [*archives_found, url]) == url


@given(st.text(), st.dictionaries(st.text(), st.none()))
def test_unique_name_is_free_and_keeps_a_free_name(name: str, taken: dict[str, object]) -> None:
    unique = style.unique_name(name, taken)
    assert unique not in taken
    # The tests run without a translation, so the default name is the English one.
    assert unique != style.DEFAULT_STYLE_NAME
    assert unique.startswith(name)
    if name not in taken and name != style.DEFAULT_STYLE_NAME:
        assert unique == name


@given(st.one_of(st.binary(), strategies.json_values.map(lambda value: json.dumps(value).encode())))
def test_parse_style_returns_a_v8_style_or_raises_value_error(raw: bytes) -> None:
    try:
        parsed = style.parse_style(raw)
    except ValueError:
        return
    assert parsed["version"] == 8


@given(styles())
def test_parse_style_reads_back_what_json_writes(raw: dict[str, object]) -> None:
    assert style.parse_style(json.dumps(raw).encode()) == raw
