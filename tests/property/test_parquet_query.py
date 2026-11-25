"""Property tests for the DuckDB queries over Parquet files."""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import geoparquet, parquet_query, query
from tests.property import strategies

if TYPE_CHECKING:
    import random


INT64 = st.integers(-(2**63), 2**63 - 1)
cell_values = st.one_of(
    strategies.hostile_text, INT64, st.floats(allow_nan=False, allow_infinity=False)
)


@given(strategies.names, cell_values)
def test_a_hostile_column_previews_and_displays_unchanged(name: str, value: str | float) -> None:
    duckdb = pytest.importorskip("duckdb")

    rows = query.Rows(query=query.Query(filter=query.Compare(name, "=", value), columns=(name,)))
    with tempfile.TemporaryDirectory() as folder, contextlib.closing(duckdb.connect()) as con:
        path = str(Path(folder) / "hostile.parquet")
        # The relation API names the column, so the file needs no SQL quoting.
        con.sql("SELECT ? AS x", params=[value]).select(
            duckdb.ColumnExpression("x").alias(name)
        ).to_parquet(path)
        plan = parquet_query.read_plan(con, path)
        assert [column.name for column in plan.columns] == [name]
        shown = parquet_query.preview(con, path, plan, rows)
        assert shown.columns == (geoparquet.output_name(name),)
        assert shown.rows == [(value,)]
        assert con.execute(parquet_query.display_select(path, plan, rows)).fetchall() == [(value,)]


@given(strategies.names, strategies.hostile_text, strategies.hostile_text)
def test_a_select_keeps_its_literals_and_loses_only_its_terminator(
    name: str, value: str, tail: str
) -> None:
    duckdb = pytest.importorskip("duckdb")

    select = f"SELECT {geoparquet.literal(value)} AS {geoparquet.quote(name)}"
    assert parquet_query.without_terminator(select) == select
    assert parquet_query.without_terminator(f"{select};{tail}") == select
    checked = parquet_query.check_select(f"{select};")
    with contextlib.closing(duckdb.connect()) as con:
        result = con.execute(checked)
        assert result.fetchall() == [(value,)]
        assert [column[0] for column in result.description] == [name]


lines = st.text(strategies.UNRESERVED + ":/?#[]@!$&'()*+,;=%", min_size=1).filter(
    lambda line: line.strip() == line
)


@given(st.one_of(lines, st.lists(lines, min_size=2, max_size=4)))
def test_source_text_round_trips(source: str | list[str]) -> None:
    assert parquet_query.source_from_text(parquet_query.source_text(source)) == source


buckets = st.builds(
    "s3://{}/{}".format,
    st.from_regex(r"[a-z0-9][a-z0-9.-]{1,20}[a-z0-9]", fullmatch=True),
    strategies.paths(),
)


@given(st.lists(buckets, min_size=1, max_size=5), st.randoms())
def test_bucket_secrets_do_not_depend_on_the_url_order(urls: list[str], rnd: random.Random) -> None:
    duckdb = pytest.importorskip("duckdb")

    secrets = parquet_query.bucket_secrets(urls)
    assert parquet_query.bucket_secrets(rnd.sample(urls, len(urls))) == secrets
    assert len(set(secrets)) == len(secrets)
    with contextlib.closing(duckdb.connect()) as con:
        for secret in secrets:
            assert len(con.extract_statements(secret)) == 1
