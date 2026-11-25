"""Property tests for reading the Portolan registry export."""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import registry
from tests import stac_builders
from tests.property import strategies

if TYPE_CHECKING:
    import random

PREFIX = "portolan_registry:"
counts = st.one_of(st.none(), st.integers(0, 10**12))


@st.composite
def catalog_links(draw: st.DrawFn) -> dict[str, object]:
    """Return one link of the registry export, with the fields the registry records."""
    link: dict[str, object] = {
        "rel": draw(st.sampled_from(("child", "child", "self", "root"))),
        "href": draw(st.one_of(strategies.folders.map("{}catalog.json".format), strategies.files)),
        "title": draw(
            st.one_of(strategies.titles, st.sampled_from(("apple", "Zulu", "Beta", "alpha")))
        ),
        "bbox": draw(st.one_of(st.none(), strategies.bboxes())),
        PREFIX + "id": draw(strategies.titles),
        PREFIX + "status": draw(st.sampled_from(("valid", "stale", "invalid", None))),
        PREFIX + "licenses": draw(
            st.dictionaries(st.text(max_size=8), st.integers(1, 9), max_size=3)
        ),
        PREFIX + "logo": draw(
            st.one_of(st.none(), st.builds(lambda href: {"href": href}, strategies.files))
        ),
    }
    for name in ("collection_count", "feature_count", "total_size_bytes"):
        link[PREFIX + name] = draw(counts)
    return link


exports = st.builds(
    lambda links: stac_builders.make_catalog("registry", other_links=links),
    st.lists(catalog_links(), max_size=6),
)


def _child_urls(export: dict[str, object]) -> list[str]:
    """Return the URL of each child link, resolved against the registry URL."""
    links = export["links"]
    assert isinstance(links, list)
    return [
        urljoin(registry.REGISTRY_URL, link["href"]) for link in links if link["rel"] == "child"
    ]


@given(exports)
def test_every_built_export_parses(export: dict[str, object]) -> None:
    entries = registry.parse_registry(export)
    assert sorted(entry.url for entry in entries) == sorted(_child_urls(export))
    titles = [entry.title.casefold() for entry in entries]
    assert titles == sorted(titles)
    assert registry.statuses(entries) == sorted({entry.status for entry in entries})


@given(exports, st.randoms(), strategies.json_texts)
def test_link_order_and_json_form_do_not_change_the_entries(
    export: dict[str, object], rnd: random.Random, dumps: strategies.Dumps
) -> None:
    links = export["links"]
    assert isinstance(links, list)
    moved = strategies.shuffled({**export, "links": rnd.sample(links, len(links))}, rnd)
    before = registry.parse_registry(export)
    after = registry.parse_registry(json.loads(dumps(moved)))
    # Entries with the same title keep the link order, so compare them as a multiset.
    assert Counter(after) == Counter(before)
    assert [entry.title.casefold() for entry in after] == [
        entry.title.casefold() for entry in before
    ]


@given(st.one_of(strategies.mutated(exports), strategies.json_values))
def test_malformed_input_raises_only_registry_error(document: object) -> None:
    try:
        entries = registry.parse_registry(document)
    except registry.RegistryError:
        return
    for entry in entries:
        entry.matches("a")
        entry.intersects((-180.0, -90.0, 180.0, 90.0))


@given(exports, st.data())
def test_any_part_of_a_field_finds_its_entry(
    export: dict[str, object], data: st.DataObject
) -> None:
    entries = registry.parse_registry(export)
    if not entries:
        return
    entry = data.draw(st.sampled_from(entries))
    field = data.draw(st.sampled_from((entry.title, entry.id, entry.url, *entry.licenses)))
    start = data.draw(st.integers(0, len(field)))
    part = field[start : data.draw(st.integers(start, len(field)))]
    assert entry in registry.filter_entries(entries, text=part)
    if part.isascii():
        assert entry in registry.filter_entries(entries, text=f" {part.swapcase()} ")
    assert registry.filter_entries(entries) == entries
    if entry.bbox is not None and entry.bbox[0] <= entry.bbox[2]:
        assert entry in registry.filter_entries(entries, bbox=entry.bbox)
    by_status = registry.filter_entries(entries, status=entry.status)
    assert all(other.status == entry.status for other in by_status)
