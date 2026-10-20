from __future__ import annotations

import pytest

from portolan_registry_qgis.core.geoparquet import (
    EVERYTHING,
    Clip,
    Selection,
    build_estimate,
    build_select,
    plan_read,
)
from portolan_registry_qgis.core.query import (
    DEFAULT_QUERY,
    And,
    Compare,
    InList,
    IsNull,
    Like,
    Not,
    Or,
    Query,
    QueryError,
    filter_sql,
    inline,
    load_text,
    pretty,
    result_plan,
    selection,
    sql_value,
)
from tests.core.test_geoparquet import LAND_USE_GEO, LAND_USE_SCHEMA

PLAN = plan_read(LAND_USE_SCHEMA, LAND_USE_GEO)


@pytest.mark.parametrize(
    ("condition", "sql", "params"),
    [
        (Compare("year", "=", 2020), '"year" = ?', [2020]),
        (Compare("c_dig1desc", "<>", "Park"), '"c_dig1desc" <> ?', ["Park"]),
        (InList("year", (1, 2)), '"year" IN (?, ?)', [1, 2]),
        (InList("year", (1,), negated=True), '"year" NOT IN (?)', [1]),
        (Like("c_dig1desc", "Res%"), '"c_dig1desc" LIKE ?', ["Res%"]),
        (Like("c_dig1desc", "res%", ignore_case=True), '"c_dig1desc" ILIKE ?', ["res%"]),
        (Like("c_dig1desc", "R%", negated=True), '"c_dig1desc" NOT LIKE ?', ["R%"]),
        (IsNull("year"), '"year" IS NULL', []),
        (IsNull("year", negated=True), '"year" IS NOT NULL', []),
        (
            Or(And(Compare("year", ">", 1), Compare("year", "<", 9)), Not(IsNull("objectid"))),
            '(("year" > ? AND "year" < ?) OR (NOT "objectid" IS NULL))',
            [1, 9],
        ),
    ],
)
def test_filter_sql_quotes_names_and_binds_values(condition, sql, params):
    assert filter_sql(condition, PLAN) == (sql, params)


def test_filter_sql_rejects_an_unknown_column():
    with pytest.raises(QueryError, match="no column named nope"):
        filter_sql(Compare("nope", "=", 1), PLAN)


def test_filter_sql_rejects_an_unknown_operator():
    with pytest.raises(QueryError, match="operator"):
        filter_sql(Compare("year", "; DROP TABLE x; --", 1), PLAN)


def test_a_value_stays_a_parameter():
    sql, params = filter_sql(Compare("c_dig1desc", "=", "x'; DROP TABLE t; --"), PLAN)
    assert sql == '"c_dig1desc" = ?'
    assert params == ["x'; DROP TABLE t; --"]


def test_selection_keeps_the_columns_and_the_filter():
    query = Query('"year" = 2020', Compare("year", "=", 2020), ("objectid",))
    assert selection(query, PLAN) == Selection(("objectid",), '"year" = ?', (2020,))
    assert selection(DEFAULT_QUERY, PLAN) is EVERYTHING
    with pytest.raises(QueryError):
        selection(Query(columns=("gone",)), PLAN)


def test_copy_keeps_only_the_chosen_columns_and_rows():
    chosen = Selection(("year",), '"year" = ?', (2020,))
    sql, params = build_select(PLAN, "MultiPolygon", Clip((0, 0, 1, 1)), chosen)
    assert '"year" AS "year"' in sql
    assert "objectid" not in sql
    assert sql.index('("year" = ?)') > sql.index("ST_MakeEnvelope")
    assert params == [0.0, 0.0, 1.0, 1.0, 2020]


def test_estimate_applies_the_filter():
    chosen = Selection(None, '"year" = ?', (2020,))
    sql, params = build_estimate(PLAN, Clip((0, 0, 1, 1)), chosen)
    assert sql.endswith('AND ("year" = ?)')
    assert params == [2020]
    table = plan_read([("id", "BIGINT")], None)
    assert build_estimate(table, selection=Selection(None, '"id" > ?', (3,))) == (
        'SELECT count(*) FROM read_parquet(?) WHERE ("id" > ?)',
        [3],
    )


def test_load_text_says_what_the_load_keeps():
    assert load_text(DEFAULT_QUERY, PLAN) == ""
    assert load_text(Query(columns=("year", "objectid")), PLAN) == "Loads 2 of 4 columns."
    assert load_text(Query('"year" IS NULL', IsNull("year")), PLAN) == (
        'Loads only rows where "year" IS NULL.'
    )
    both = Query('"year" IS NULL', IsNull("year"), ("year",))
    assert load_text(both, PLAN) == 'Loads 1 of 4 columns, only rows where "year" IS NULL.'
    assert load_text(Query(sql="SELECT 1"), PLAN) == (
        "Loads with custom SQL. The area of interest does not apply."
    )
    assert Query(sql="SELECT 1").is_default is False
    assert DEFAULT_QUERY.is_default


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, "NULL"),
        (True, "TRUE"),
        (3, "3"),
        (-2.5, "-2.5"),
        ("it's", "'it''s'"),
        (["a", "b"], "['a', 'b']"),
    ],
)
def test_sql_value(value, text):
    assert sql_value(value) == text


def test_inline_skips_marks_in_literals_and_names():
    sql = "SELECT '?', \"a?\" FROM read_parquet(?) WHERE x = ? AND y = 'it''s ?'"
    assert inline(sql, ["f.parquet", 1]) == (
        "SELECT '?', \"a?\" FROM read_parquet('f.parquet') WHERE x = 1 AND y = 'it''s ?'"
    )
    with pytest.raises(ValueError, match="parameters"):
        inline("SELECT ?", [])
    with pytest.raises(ValueError, match="marks"):
        inline("SELECT 1", [1])


def test_pretty_breaks_only_the_top_level():
    sql = "SELECT a, f(b, c) AS d, 'x, y' FROM t WHERE a = 1 AND (b = 2 AND c = 3)"
    assert pretty(sql) == (
        "SELECT\n  a,\n  f(b, c) AS d,\n  'x, y'\nFROM t\nWHERE a = 1\n  AND (b = 2 AND c = 3)"
    )


def test_result_plan_needs_the_geometry_column():
    staged = result_plan(PLAN, [("year", "SMALLINT"), ("geometry", "GEOMETRY")])
    assert staged.geometry == "geometry"
    assert staged.geometry_type == "Unknown"
    assert [column.name for column in staged.columns] == ["year"]
    assert result_plan(PLAN, [("geometry", "BLOB")]).geometry_is_wkb_blob
    with pytest.raises(QueryError, match="named geometry"):
        result_plan(PLAN, [("year", "SMALLINT")])
    with pytest.raises(QueryError, match="named geometry"):
        result_plan(PLAN, [("geometry", "VARCHAR")])
    table = plan_read([("id", "BIGINT")], None)
    assert not result_plan(table, [("n", "BIGINT")]).has_geometry


def test_a_filter_value_must_be_text_or_a_number():
    with pytest.raises(QueryError, match="text or a number"):
        filter_sql(Compare("year", "=", [1, 2]), PLAN)


def test_selection_of_a_filter_alone_keeps_every_column():
    query = Query('"year" = 1', Compare("year", "=", 1))
    assert selection(query, PLAN) == Selection(None, '"year" = ?', (1,))
    with pytest.raises(QueryError):
        selection(Query(sql="SELECT 1"), PLAN)


def test_pretty_leaves_a_statement_that_is_not_a_select_on_one_list():
    assert pretty("COPY t TO 'x', y FROM z") == "COPY t TO 'x', y\nFROM z"


def test_an_unknown_layer_type_keeps_every_geometry():
    sql, _params = build_select(PLAN, "Unknown")
    assert sql.startswith('SELECT "geometry" AS "geometry"')
    assert "CASE WHEN" not in sql
