"""Property tests for planning a download."""

from __future__ import annotations

import operator
import re
import string
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import download
from tests import stac_builders
from tests.property import strategies

if TYPE_CHECKING:
    from collections.abc import Callable

_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._@+=-]+")
ROOT = "https://x.test/cat/catalog.json"
safe_paths = st.lists(
    st.text(string.ascii_letters + string.digits + "-._", min_size=1, max_size=6).filter(
        lambda segment: segment not in {".", ".."}
    ),
    min_size=1,
    max_size=4,
).map("/".join)
# Path text that tries to climb above the target folder.
climbs = st.sampled_from(
    ("..", "../../etc/passwd", "%2e%2E/%2E%2e/x", "a/./../../b", "..%2F..%2Fx")
)
tails = st.one_of(strategies.hostile_text, climbs)
urls = st.one_of(
    st.builds(operator.add, strategies.folders, tails),
    st.builds("https://x.test/cat/{}".format, tails),
)


def _stays_inside(path: str) -> bool:
    """Return whether ``path`` is relative and names no folder above its start."""
    segments = path.split("/")
    return all(_SAFE_SEGMENT.fullmatch(s) and s not in {".", ".."} for s in segments)


@given(urls, st.one_of(st.just(ROOT), strategies.folders))
def test_no_local_path_climbs_out_of_the_folder(url: str, root: str) -> None:
    assert _stays_inside(download.local_path(url, root))
    clipped = download.clipped_path(url, root)
    assert _stays_inside(clipped)
    assert clipped.endswith(".gpkg")


@given(safe_paths, strategies.folders)
def test_a_file_below_the_root_keeps_its_relative_path(path: str, folder: str) -> None:
    # core.md, Links: "Relative links keep a catalog portable, so the same
    # tree of files validates locally, in staging, and in production without
    # a rewrite."
    assert download.local_path(folder + path, folder + "catalog.json") == path
    clipped = download.clipped_path(f"{folder}{path}.parquet", folder + "catalog.json")
    assert clipped == f"{path}.gpkg"


planned = st.builds(
    download.PlannedFile,
    url=strategies.hostile_text,
    path=st.sampled_from(("a/B.tif", "a/b.TIF", "c", "C", "d")),
)


@given(st.lists(planned, max_size=8, unique_by=lambda planned: planned.url))
def test_split_collisions_keeps_the_first_file_of_each_path(
    files: list[download.PlannedFile],
) -> None:
    keep, clashes = download.split_collisions(files)
    assert len(keep) + len(clashes) == len(files)
    assert [planned for planned in files if planned in keep] == keep
    assert [planned for planned in files if planned in clashes] == clashes
    first = {planned.path.casefold(): planned for planned in reversed(files)}
    assert keep == [planned for planned in files if planned in first.values()]


@st.composite
def catalogs(draw: st.DrawFn) -> dict[str, object]:
    """Return linked catalogs by URL. Some links point to a missing document or to an ancestor."""
    count = draw(st.integers(1, 8))
    documents: dict[str, object] = {}
    for i in range(count):
        children = draw(st.lists(st.integers(0, count), max_size=3))
        relative = [f"../n{child}/catalog.json" for child in children]
        documents[f"https://x.test/n{i}/catalog.json"] = stac_builders.make_catalog(
            f"n{i}", relative
        )
    return documents


def _fetch(documents: dict[str, object]) -> Callable[[str], object]:
    def fetch(url: str) -> object:
        if url not in documents:
            message = f"404 {url}"
            raise OSError(message)
        return documents[url]

    return fetch


@given(catalogs())
def test_reading_ahead_gives_the_plan_of_reading_in_turn(documents: dict[str, object]) -> None:
    # Within the read budget, the order of the reads does not change the plan.
    start = next(iter(documents))
    in_turn = download.plan(_fetch(documents), start)
    with ThreadPoolExecutor(4) as executor:
        ahead = download.plan(_fetch(documents), start, how=download.Walk(executor=executor))
    assert ahead == in_turn
    assert len({planned.url for planned in in_turn.files}) == len(in_turn.files)
