"""Property tests for choosing and describing the files a document offers."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import download, hrefs, loadable, scope, stac
from tests.property import strategies

if TYPE_CHECKING:
    import random

loadables = st.builds(
    stac.Loadable,
    href=st.one_of(strategies.markup, strategies.hostile_text),
    title=st.text(max_size=6),
    format=st.sampled_from(stac.PREFERRED_FORMATS),
    layers=st.lists(strategies.markup, max_size=3).map(tuple),
)
views = st.builds(scope.MapView, st.just((-10.0, -10.0, 10.0, 10.0)))


@given(items=st.lists(loadables, max_size=6), duckdb_ok=st.booleans(), rnd=st.randoms())
def test_the_buttons_pick_by_format_in_any_file_order(
    items: list[stac.Loadable], rnd: random.Random, *, duckdb_ok: bool
) -> None:
    targets = loadable.pick_targets(tuple(items), duckdb_ok=duckdb_ok)
    formats = {item.format for item in items}
    assert (targets.view is None) == formats.isdisjoint(loadable.VIEW_FORMATS)
    assert (targets.load is None) == formats.isdisjoint(loadable.LOAD_FORMATS)
    if targets.view is not None:
        assert targets.view.format == next(f for f in loadable.VIEW_FORMATS if f in formats)
    if targets.load is not None and not duckdb_ok and formats & {"flatgeobuf", "geojson", "csv"}:
        assert targets.load.format != "parquet"
    moved = loadable.pick_targets(tuple(rnd.sample(items, len(items))), duckdb_ok=duckdb_ok)
    assert [t.format if t else None for t in moved] == [t.format if t else None for t in targets]


@given(item=loadables, on_map=st.booleans())
def test_a_file_tip_escapes_every_value(item: stac.Loadable, *, on_map: bool) -> None:
    tip = loadable.loadable_tip(item, on_map=on_map)
    lines = tip.split("<br>")
    assert html.unescape(lines[0]) == item.href
    assert "<" not in tip.replace("<br>", "")
    assert len(lines) == 1 + bool(item.layers) + on_map


@given(loadables, st.lists(strategies.hostile_text, max_size=3))
def test_a_file_is_on_the_map_in_either_path_form(item: stac.Loadable, sources: list[str]) -> None:
    assert loadable.is_on_map(item, {item.href}, [])
    for path in (hrefs.vsicurl_path(item.href), hrefs.plain_vsicurl_path(item.href)):
        assert loadable.is_on_map(item, set(), [*sources, f"{path}|layername=x"])


@given(strategies.documents(), strategies.folders, st.one_of(views, st.just(scope.Everything())))
def test_parquet_to_clip_takes_the_parquet_files_in_the_area(
    raw: dict[str, object], folder: str, chosen: scope.Scope
) -> None:
    document = stac.read_document(raw, folder + "collection.json")
    parquet = [
        item.asset
        for item in document.loadable()
        if item.asset is not None and item.format == "parquet" and item.asset.read_url is not None
    ]
    clip = loadable.parquet_to_clip(document, chosen)
    if isinstance(chosen, scope.Everything):
        assert clip == []
    else:
        assert clip == [asset for asset in parquet if not asset.tabular]
    files = [download.PlannedFile(asset.href, asset.href) for asset in parquet]
    plan = loadable.split_plan(download.Plan([*files, download.PlannedFile(folder, "x")], []), clip)
    clipped = {asset.href for asset in clip}
    kept = [asset.href for asset in parquet if asset.href not in clipped]
    assert [entry.url for entry in plan.files] == [*kept, folder]
