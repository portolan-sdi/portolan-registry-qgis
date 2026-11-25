"""Property tests for reading STAC documents."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import stac
from tests.property import strategies

if TYPE_CHECKING:
    import random

FIXTURES = Path(__file__).parent.parent / "fixtures" / "anncsu"
FIXTURE_BASE = "https://pub-1e760dc850cb4a5aa5f8afb77713f8cd.r2.dev/"
KINDS = {"Catalog": "catalog", "Collection": "collection", "Feature": "item"}
NAME = "document.json"


def _touch(document: stac.Document) -> None:
    """Read every derived property of ``document``, so a fault in one raises."""
    document.loadable()
    document.preferred_href()
    document.preferred_href(parquet=False)
    _ = (document.styles, document.default_style, document.data_assets, document.icon)
    _ = (document.thumbnail, document.pmtiles_duplicates)


def _assets_by_key(document: stac.Document) -> stac.Document:
    return replace(document, assets=tuple(sorted(document.assets, key=lambda a: (a.key, a.href))))


def _fixtures() -> list[tuple[dict[str, object], str]]:
    found = []
    for name in ("catalog.json", "indirizzi/collection.json"):
        document = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        found.append((document, FIXTURE_BASE + name))
    return found


@given(strategies.documents(), strategies.folders)
def test_every_built_document_parses(raw: dict[str, object], folder: str) -> None:
    document = stac.read_document(raw, folder + NAME)
    _touch(document)
    assert document.kind == KINDS[str(raw["type"])]
    assert document.id == raw["id"]
    links = raw["links"]
    assert isinstance(links, list)
    assert len(document.children) == sum(link["rel"] in {"child", "item"} for link in links)
    raw_assets = raw.get("assets", {})
    assert isinstance(raw_assets, dict)
    assert len(document.assets) == len(raw_assets)
    assert all(asset.href.startswith(folder) for asset in document.assets)
    assert document.bbox == _corners(raw)


def _corners(raw: dict[str, object]) -> stac.Bbox | None:
    """Return the west, south, east, and north of the bbox that ``raw`` declares.

    RFC 7946 section 5: the axes of the most southwesterly point come first,
    then those of the more northeasterly point.
    """
    extent = raw.get("extent")
    values = extent["spatial"]["bbox"][0] if isinstance(extent, dict) else raw.get("bbox")
    if not isinstance(values, list):
        return None
    half = len(values) // 2
    return (values[0], values[1], values[half], values[half + 1])


@given(st.sampled_from(_fixtures()), strategies.json_texts, st.randoms())
def test_fixtures_parse_in_any_json_form(
    fixture: tuple[dict[str, object], str], dumps: strategies.Dumps, rnd: random.Random
) -> None:
    raw, href = fixture
    # RFC 8259 section 2: "Insignificant whitespace is allowed before or after
    # any of the six structural characters." Section 4: "An object is an
    # unordered collection of zero or more name/value pairs". Section 7: any
    # character may be escaped. The asset order follows the member order.
    text = dumps(strategies.shuffled(raw, rnd))
    assert _assets_by_key(stac.read_document(json.loads(text), href)) == _assets_by_key(
        stac.read_document(raw, href)
    )


@given(strategies.documents(), strategies.json_texts)
def test_json_form_does_not_change_the_parse(
    raw: dict[str, object], dumps: strategies.Dumps
) -> None:
    href = "https://x.test/a/" + NAME
    parsed = stac.read_document(json.loads(dumps(raw)), href)
    assert _assets_by_key(parsed) == _assets_by_key(stac.read_document(raw, href))


@given(st.one_of(strategies.mutated(strategies.documents()), strategies.json_values))
def test_malformed_input_raises_only_stac_error(raw: object) -> None:
    try:
        document = stac.read_document(raw, "https://x.test/a/" + NAME)
    except stac.StacError:
        return
    _touch(document)


@given(
    st.lists(strategies.json_numbers, min_size=4, max_size=6), st.sampled_from(("bbox", "extent"))
)
def test_a_bbox_of_any_numbers_reads_or_drops(values: list[object], where: str) -> None:
    # core.md, Bounding Boxes: every bbox MUST "contain no `NaN` or infinite
    # values" and "contain no sentinel 'effectively infinite' values". A
    # reader drops such a bbox and keeps the document.
    raw: dict[str, object]
    if where == "bbox":
        raw = {"type": "Feature", "id": "a", "bbox": values}
    else:
        raw = {"type": "Collection", "id": "a", "extent": {"spatial": {"bbox": [values]}}}
    bbox = stac.read_document(raw, "https://x.test/" + NAME).bbox
    assert bbox is None or all(abs(value) < float("inf") for value in bbox)


@given(strategies.documents(), st.randoms())
def test_member_order_does_not_change_the_parse(raw: dict[str, object], rnd: random.Random) -> None:
    # RFC 8259 section 4: "An object is an unordered collection of zero or
    # more name/value pairs".
    href = "https://x.test/a/" + NAME
    expected = stac.read_document(raw, href)
    kept = stac.read_document(strategies.shuffled(raw, rnd, skip=frozenset({"assets"})), href)
    assert kept == expected
    moved = stac.read_document(strategies.shuffled(raw, rnd), href)
    assert _assets_by_key(moved) == _assets_by_key(expected)
    # core.md, Visualization Styles: "Because STAC assets are an unordered
    # JSON object, and because STAC states that asset keys carry no meaning a
    # client is expected to understand, the default style is identified by a
    # second role rather than by position or key".
    defaults = [a for a in expected.styles if "default" in a.roles]
    if len(defaults) <= 1:
        assert moved.default_style == expected.default_style
    assert _preferred_format(moved) == _preferred_format(expected)


def _preferred_format(document: stac.Document) -> str | None:
    """Return the format of the file that ``preferred_href`` picks.

    Two assets can share an href with different formats. The pick is the best ranked.
    """
    href = document.preferred_href()
    formats = [item.format for item in document.loadable() if item.href == href]
    return min(formats, key=stac.PREFERRED_FORMATS.index, default=None)


@given(strategies.documents(), st.data())
def test_renaming_an_asset_key_changes_only_that_key(
    raw: dict[str, object], data: st.DataObject
) -> None:
    raw_assets = raw.get("assets")
    if not isinstance(raw_assets, dict) or not raw_assets:
        return
    old = data.draw(st.sampled_from(sorted(raw_assets)))
    new = data.draw(st.text(min_size=1).filter(lambda key: key not in raw_assets))
    renamed = {**raw, "assets": {(new if key == old else key): v for key, v in raw_assets.items()}}
    href = "https://x.test/a/" + NAME
    before, after = stac.read_document(raw, href), stac.read_document(renamed, href)
    # core.md, Visualization Styles: "STAC states that asset keys carry no
    # meaning a client is expected to understand".
    assert after.assets == tuple(
        replace(asset, key=new) if asset.key == old else asset for asset in before.assets
    )
    assert replace(after, assets=before.assets) == before
    assert after.preferred_href() == before.preferred_href()
    if before.default_style is not None:
        assert after.default_style is not None
        assert replace(after.default_style, key=before.default_style.key) == before.default_style


@given(strategies.documents(), strategies.folders, strategies.folders)
def test_relative_and_absolute_hrefs_parse_the_same(
    raw: dict[str, object], folder: str, redirect: str
) -> None:
    # core.md, Assets: "The `href` MAY be relative or absolute."
    # core.md, Links: "Structural links can be relative or absolute; Portolan
    # takes no position."
    href, base = folder + NAME, redirect + NAME
    absolute = _absolute(raw, base)
    assert stac.read_document(absolute, href) == stac.read_document(raw, href, base)


def _absolute(raw: dict[str, object], base: str) -> dict[str, object]:
    """Return ``raw`` with every link and asset href resolved against ``base``."""
    links = raw["links"]
    assert isinstance(links, list)
    absolute: dict[str, object] = {
        **raw,
        "links": [{**link, "href": urljoin(base, link["href"])} for link in links],
    }
    assets = raw.get("assets")
    if isinstance(assets, dict):
        absolute["assets"] = {
            key: {**asset, "href": urljoin(base, asset["href"])} for key, asset in assets.items()
        }
    return absolute
