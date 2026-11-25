"""Property tests for reading item files from a stac-geoparquet item mirror."""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import mirror, parquet_query, scope
from tests import duck
from tests.property import strategies

# Whole degrees, so DuckDB and Python compare the same values.
boxes = strategies.boxes(st.integers(-180, 180).map(float), st.integers(-90, 90).map(float))


@given(st.lists(boxes, min_size=1, max_size=8), boxes)
def test_the_mirror_query_keeps_every_item_in_the_view(
    items: list[strategies.Box], view: strategies.Box
) -> None:
    pytest.importorskip("duckdb")

    chosen = scope.MapView(view)
    rows = [{"xmin": w, "ymin": s, "xmax": e, "ymax": n} for w, s, e, n in items]
    with tempfile.TemporaryDirectory() as folder, contextlib.closing(duck.connect()) as con:
        path = str(Path(folder) / "items.parquet")
        con.execute(
            "CREATE TABLE m (id VARCHAR, "
            "bbox STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE), assets VARCHAR)"
        )
        con.executemany(
            "INSERT INTO m VALUES (?, ?, '{}')", [[str(i), r] for i, r in enumerate(rows)]
        )
        con.execute(f"COPY m TO '{path}' (FORMAT parquet)")
        sql, params = mirror.mirror_query(parquet_query.read_plan(con, path), chosen)
        found = {row[-1] for row in con.execute(sql, [path, *params]).fetchall()}
    inside = {str(i) for i, box in enumerate(items) if scope.intersects(box, chosen)}
    # The query keeps a row across the antimeridian for row_href to test.
    across = {str(i) for i, box in enumerate(items) if box[0] > box[2]}
    assert inside <= found
    if view[0] <= view[2]:
        assert found - across == inside - across
    else:
        assert found == {str(i) for i in range(len(items))}


@given(st.text())
def test_skipped_path_reads_back_the_path_of_a_gdal_message(path: str) -> None:
    assert mirror.skipped_path(f"Can't open {path}. Skipping it") == path


@given(st.lists(st.one_of(strategies.files, strategies.folders.map("{}a.tif".format)), max_size=6))
def test_split_files_divides_the_files_in_order(files: list[str]) -> None:
    hrefs = [f"https://x.test/{name}" if "://" not in name else name for name in files]
    cogs, tables = mirror.split_files(hrefs)
    assert sorted(cogs + tables) == sorted(hrefs)
    assert all(href.endswith(".parquet") for href in tables)
    assert cogs == [href for href in hrefs if href in cogs]
    assert tables == [href for href in hrefs if href in tables]
