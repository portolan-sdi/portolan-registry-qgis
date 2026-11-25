"""Property tests for the Details table of a document."""

from __future__ import annotations

import html
import re
from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import facts, stac
from tests import stac_builders
from tests.property import strategies

RELS = ("license", "describedby", "via", "self")


# A "<" in the table opens a tag, because the table escapes every value.
_TAG = re.compile(r"</?([^\s>]*)")
_ANCHOR = re.compile(r'<a href="([^"]*)">')


@st.composite
def documents(draw: st.DrawFn) -> stac.Document:
    """Return a document whose titles, license, and link hrefs are hostile text."""
    links = draw(
        st.lists(
            st.builds(
                lambda rel, href, title: {"rel": rel, "href": href, "title": title},
                st.sampled_from(RELS),
                st.one_of(strategies.markup, strategies.files),
                strategies.markup,
            ),
            max_size=5,
        )
    )
    license_value = draw(st.one_of(st.none(), strategies.markup))
    raw = stac_builders.make_collection("c", other_links=links, license=license_value)
    return stac.read_document(raw, "https://x.test/c/collection.json")


@given(
    documents(),
    st.one_of(st.none(), strategies.markup),
    st.one_of(st.none(), strategies.markup),
)
def test_hostile_text_adds_no_markup(
    document: stac.Document, publisher: str | None, browser: str | None
) -> None:
    table = facts.facts_html(document, publisher, None, "#7a7a72", browser)
    assert set(_TAG.findall(table)) <= {"table", "tr", "td", "a"}
    linked = {link.href for link in document.links if link.rel in {"license", "describedby", "via"}}
    expected = linked | ({browser} if browser else set())
    assert {html.unescape(href) for href in _ANCHOR.findall(table)} == expected
    if publisher:
        assert f"<td>{html.escape(publisher)}</td>" in table


@given(documents(), st.lists(st.one_of(st.none(), st.integers(0, 10**15)), max_size=4))
def test_document_size_sums_the_declared_sizes(
    document: stac.Document, sizes: list[int | None]
) -> None:
    assets = tuple(
        stac.Asset(str(n), f"https://x.test/{n}", None, None, (), size, None, None)
        for n, size in enumerate(sizes)
    )
    sized = [size for size in sizes if size is not None]
    total = facts.document_size(replace(document, assets=assets), None)
    assert total == (sum(sized) if sized else None)
