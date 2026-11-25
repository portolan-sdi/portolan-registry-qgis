"""Property tests for the SQL that a row filter builds."""

from __future__ import annotations

import contextlib
import math
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import geoparquet, query
from tests.property import strategies

if TYPE_CHECKING:
    import duckdb


# The range of DuckDB's HUGEINT, the widest integer a literal reads as.
HUGEINT = st.integers(-(2**127) + 1, 2**127 - 1)
SQL_VALUES: dict[str, st.SearchStrategy[object]] = {
    "null or bool": st.one_of(st.none(), st.booleans()),
    "integer": HUGEINT,
    "float": st.floats(),
    "text": strategies.hostile_text,
    "integer list": st.lists(HUGEINT, max_size=4),
    "text list": st.lists(strategies.hostile_text, max_size=4),
}
Rows = list[tuple[object, ...]] | str


def _same(value: object, got: object) -> bool:
    """Return whether DuckDB's ``got`` is the Python ``value``."""
    if isinstance(value, float):
        number = float(got) if isinstance(got, (float, Decimal)) else math.nan
        return number == value or (math.isnan(value) and math.isnan(number))
    return got == value and type(got) is type(value)


@pytest.mark.parametrize("kind", SQL_VALUES)
@given(data=st.data())
def test_a_sql_value_reads_back_as_the_same_value(kind: str, data: st.DataObject) -> None:
    duckdb = pytest.importorskip("duckdb")

    value = data.draw(SQL_VALUES[kind])
    with contextlib.closing(duckdb.connect()) as con:
        row = con.execute(f"SELECT {query.sql_value(value)}").fetchone()
    assert row is not None
    assert _same(value, row[0])


def _conditions(column: str) -> st.SearchStrategy[query.Condition]:
    text = strategies.hostile_text
    leaves: st.SearchStrategy[query.Condition] = st.one_of(
        st.builds(query.Compare, st.just(column), st.sampled_from(query.COMPARISONS), text),
        st.builds(
            query.InList,
            st.just(column),
            st.lists(text, min_size=1, max_size=3).map(tuple),
            st.booleans(),
        ),
        st.builds(query.Like, st.just(column), text, st.booleans(), st.booleans()),
        st.builds(query.IsNull, st.just(column), st.booleans()),
    )
    return st.recursive(
        leaves,
        lambda inner: st.one_of(
            st.builds(query.And, inner, inner),
            st.builds(query.Or, inner, inner),
            st.builds(query.Not, inner),
        ),
        max_leaves=4,
    )


def _table(con: duckdb.DuckDBPyConnection, name: str, values: list[str | None]) -> None:
    """Create table ``h`` with one text column named ``name``, without SQL quoting."""
    duckdb = pytest.importorskip("duckdb")

    con.execute("CREATE TABLE v AS SELECT unnest(?::VARCHAR[]) AS x", [values])
    con.table("v").select(duckdb.ColumnExpression("x").alias(name)).to_table("h")


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[object]) -> Rows:
    """Return the rows of ``sql``, or the name of the error it raises."""
    duckdb = pytest.importorskip("duckdb")

    try:
        return con.execute(sql, params).fetchall()
    except duckdb.Error as error:
        return type(error).__name__


@given(
    strategies.names,
    st.lists(st.one_of(st.none(), strategies.hostile_text), min_size=1, max_size=4),
    st.data(),
)
def test_a_filter_selects_the_same_rows_bound_inlined_and_pretty(
    name: str, values: list[str | None], data: st.DataObject
) -> None:
    duckdb = pytest.importorskip("duckdb")

    condition = data.draw(_conditions(name))
    plan = geoparquet.plan_read([(name, "VARCHAR")], None)
    where, params = query.filter_sql(condition, plan)
    select = f"SELECT {geoparquet.quote(name)} FROM h WHERE {where} ORDER BY ALL"
    with contextlib.closing(duckdb.connect()) as con:
        _table(con, name, values)
        bound = _rows(con, select, params)
        inlined = query.inline(select, params)
        assert _rows(con, inlined, []) == bound
        assert _rows(con, query.pretty(inlined), []) == bound


@given(strategies.names, strategies.hostile_text)
def test_a_hostile_name_and_value_stay_inside_their_quotes(name: str, value: str) -> None:
    duckdb = pytest.importorskip("duckdb")

    plan = geoparquet.plan_read([(name, "VARCHAR")], None)
    where, params = query.filter_sql(query.Compare(name, "=", value), plan)
    select = f"SELECT * FROM h WHERE {where}"
    with contextlib.closing(duckdb.connect()) as con:
        _table(con, name, [value])
        assert con.execute(select, params).fetchall() == [(value,)]
        assert con.execute(query.inline(select, params)).fetchall() == [(value,)]
        assert [column[0] for column in con.execute(select, params).description] == [name]
        assert con.execute("SELECT count(*) FROM duckdb_tables()").fetchall() == [(2,)]


@given(st.lists(strategies.names, max_size=3), strategies.names)
def test_selection_refuses_only_unknown_columns(present: list[str], wanted: str) -> None:
    plan = geoparquet.plan_read(
        [(name, "VARCHAR") for name in dict.fromkeys([*present, "k"])], None
    )
    known = {column.name for column in plan.columns}
    picked = query.Query(columns=(wanted,))
    if wanted in known:
        assert query.selection(picked, plan).columns == (wanted,)
    else:
        with pytest.raises(query.QueryError):
            query.selection(picked, plan)
