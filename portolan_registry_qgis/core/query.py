"""Describe the rows and columns of a GeoParquet copy, and the SQL it runs.

A row filter is a tree of conditions. The SQL quotes every column name and
binds every value as a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass

from portolan_registry_qgis.core.geoparquet import (
    EVERYTHING,
    NO_GEOMETRY,
    UNKNOWN,
    WHOLE_FILE,
    Clip,
    Column,
    ReadPlan,
    Selection,
    field_kind,
    literal,
    quote,
)
from portolan_registry_qgis.core.i18n import _, ngettext, number

# A value that a filter compares a column with.
Value = str | int | float | bool
COMPARISONS = ("=", "<>", "<", "<=", ">", ">=")
# Marks the end of the parameters in ``inline``.
_END = object()


class QueryError(ValueError):
    """The filter, the columns, or the SQL of a query does not fit the file."""


@dataclass(frozen=True)
class Compare:
    """A column compared with a value."""

    column: str
    op: str
    value: Value


@dataclass(frozen=True)
class InList:
    """A column whose value is, or is not, one of a list."""

    column: str
    values: tuple[Value, ...]
    negated: bool = False


@dataclass(frozen=True)
class Like:
    """A column that matches, or does not match, a ``LIKE`` pattern."""

    column: str
    pattern: str
    negated: bool = False
    ignore_case: bool = False


@dataclass(frozen=True)
class IsNull:
    """A column that is, or is not, NULL."""

    column: str
    negated: bool = False


@dataclass(frozen=True)
class And:
    """Two conditions that both hold."""

    left: Condition
    right: Condition


@dataclass(frozen=True)
class Or:
    """Two conditions of which at least one holds."""

    left: Condition
    right: Condition


@dataclass(frozen=True)
class Not:
    """A condition that does not hold."""

    operand: Condition


Condition = Compare | InList | Like | IsNull | And | Or | Not


@dataclass(frozen=True)
class Query:
    """The rows and columns a copy keeps, or the SELECT that replaces them.

    ``filter_text`` is the QGIS expression the user typed, and ``filter`` its
    conditions. ``columns`` None keeps every column. ``sql`` replaces the rest.
    """

    filter_text: str = ""
    filter: Condition | None = None
    columns: tuple[str, ...] | None = None
    sql: str | None = None

    @property
    def is_default(self) -> bool:
        """Whether the query keeps every row and column of the area."""
        return self.sql is None and self.filter is None and self.columns is None


# The query of a copy that keeps every row and column of the area.
DEFAULT_QUERY = Query()


@dataclass(frozen=True)
class Rows:
    """The part of a file that a count, a preview, or a copy reads."""

    clip: Clip = WHOLE_FILE
    query: Query = DEFAULT_QUERY


# Every row and column of the file.
ALL_ROWS = Rows()


def _names(plan: ReadPlan) -> set[str]:
    return {column.name for column in plan.columns}


def _column(name: str, names: set[str]) -> str:
    if name not in names:
        raise QueryError(_("The file has no column named {name}").format(name=name))
    return quote(name)


def _check_value(value: object) -> Value:
    if not isinstance(value, (str, int, float, bool)):
        raise QueryError(_("A filter compares a column with text or a number only"))
    return value


def _condition_sql(condition: Condition, names: set[str], params: list[object]) -> str:
    """Return the SQL of ``condition``, and add its values to ``params``."""
    if isinstance(condition, (And, Or)):
        word = "AND" if isinstance(condition, And) else "OR"
        left = _condition_sql(condition.left, names, params)
        return f"({left} {word} {_condition_sql(condition.right, names, params)})"
    if isinstance(condition, Not):
        return f"(NOT {_condition_sql(condition.operand, names, params)})"
    return _test_sql(condition, _column(condition.column, names), params)


def _test_sql(
    condition: Compare | InList | Like | IsNull, column: str, params: list[object]
) -> str:
    """Return the SQL of a test on the quoted ``column``, and add its values to ``params``."""
    negated = "NOT " if not isinstance(condition, Compare) and condition.negated else ""
    if isinstance(condition, IsNull):
        return f"{column} IS {negated}NULL"
    if isinstance(condition, Compare):
        if condition.op not in COMPARISONS:
            raise QueryError(_("A filter cannot use the operator {op}").format(op=condition.op))
        params.append(_check_value(condition.value))
        return f"{column} {condition.op} ?"
    if isinstance(condition, Like):
        params.append(condition.pattern)
        return f"{column} {negated}{'ILIKE' if condition.ignore_case else 'LIKE'} ?"
    params.extend(_check_value(value) for value in condition.values)
    return f"{column} {negated}IN ({', '.join('?' for _value in condition.values)})"


def filter_sql(condition: Condition, plan: ReadPlan) -> tuple[str, list[object]]:
    """Return the SQL condition and the parameters of ``condition`` on ``plan``'s file.

    Raises:
        QueryError: A condition names a column the file does not hold.
    """
    params: list[object] = []
    return _condition_sql(condition, _names(plan), params), params


def selection(query: Query, plan: ReadPlan) -> Selection:
    """Return the selection that ``query`` makes on ``plan``'s file.

    Raises:
        QueryError: The query names a column the file does not hold.
    """
    if query.sql is not None:
        raise QueryError("Custom SQL has no selection")  # i18n: skip - a caller bug
    if query.columns is None and query.filter is None:
        return EVERYTHING
    names = _names(plan)
    if query.columns is not None:
        for name in query.columns:
            _column(name, names)
    if query.filter is None:
        return Selection(query.columns)
    where, params = filter_sql(query.filter, plan)
    return Selection(query.columns, where, tuple(params))


def result_plan(plan: ReadPlan, schema: list[tuple[str, str]]) -> ReadPlan:
    """Plan the copy of a custom SELECT on ``plan``'s file, whose columns are ``schema``.

    Raises:
        QueryError: The file has geometry and the SELECT does not return its column.
    """
    geometry = plan.geometry
    types = dict(schema)
    if geometry is not None and not (
        types.get(geometry, "").upper().startswith("GEOMETRY")
        or types.get(geometry, "").upper() == "BLOB"
    ):
        raise QueryError(
            _("The SQL must return the geometry in a column named {name}").format(name=geometry)
        )
    columns = tuple(
        Column(name, duckdb_type, field_kind(duckdb_type))
        for name, duckdb_type in schema
        if name != geometry
    )
    return ReadPlan(
        geometry=geometry,
        geometry_is_wkb_blob=geometry is not None and types[geometry].upper() == "BLOB",
        crs=plan.crs,
        geometry_type=UNKNOWN if geometry is not None else NO_GEOMETRY,
        columns=columns,
        covering=None,
    )


def load_text(query: Query, plan: ReadPlan | None) -> str:
    """Return the sentence that says what a load with ``query`` keeps, or "" for the default."""
    if query.sql is not None:
        return _("Loads with custom SQL. The area of interest does not apply.")
    columns = None
    if query.columns is not None and plan is not None:
        total = len(plan.columns)
        # Translators: {kept} columns of the {n} in the file go into the load.
        text = ngettext("{kept} of {n} column", "{kept} of {n} columns", total)
        columns = text.format(kept=number(len(query.columns)), n=number(total))
    rows = query.filter_text if query.filter is not None else None
    if columns and rows:
        # Translators: {filter} is the QGIS expression that the rows match.
        return _("Loads {columns}, only rows where {filter}.").format(columns=columns, filter=rows)
    if columns:
        return _("Loads {columns}.").format(columns=columns)
    if rows:
        return _("Loads only rows where {filter}.").format(filter=rows)
    return ""


def sql_value(value: object) -> str:
    """Return ``value`` as a SQL literal that DuckDB reads back as the same value."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(sql_value(item) for item in value) + "]"
    return literal(str(value))


# The top-level words that start a new line in ``pretty``, and the text that replaces each.
_BREAKS = (
    (" FROM ", "\nFROM "),
    (" WHERE ", "\nWHERE "),
    (" AND ", "\n  AND "),
    (", ", ",\n  "),
)


def _break_at(sql: str, index: int, *, in_list: bool) -> tuple[str, str] | None:
    """Return the break that starts at ``index`` of top-level ``sql``, or None."""
    found = next((pair for pair in _BREAKS if sql.startswith(pair[0], index)), None)
    if found is None or (found[0] == ", " and not in_list):
        return None
    return found


def _scan(char: str, closing: str | None, depth: int) -> tuple[str | None, int]:
    """Return the quote that ``char`` leaves open and the parenthesis depth after it."""
    if closing is not None:
        return (None if char == closing else closing), depth
    if char in "'\"":
        return char, depth
    return None, depth + (char == "(") - (char == ")")


def pretty(sql: str) -> str:
    """Break a one-line SELECT that the plugin built into lines, for people to read.

    Only text outside parentheses, string literals, and quoted names breaks.
    The SELECT list puts one column on each line.
    """
    out: list[str] = []
    depth, closing, index = 0, None, 0
    in_list = sql.startswith("SELECT ")
    if in_list:
        out.append("SELECT\n  ")
        index = len("SELECT ")
    while index < len(sql):
        found = _break_at(sql, index, in_list=in_list) if closing is None and depth == 0 else None
        if found is not None:
            in_list = in_list and found[0] == ", "
            out.append(found[1])
            index += len(found[0])
            continue
        closing, depth = _scan(sql[index], closing, depth)
        out.append(sql[index])
        index += 1
    return "".join(out)


def inline(sql: str, params: list[object]) -> str:
    """Put ``params`` into the ``?`` marks of ``sql`` as literals, outside quotes only.

    Raises:
        ValueError: The count of marks and parameters differ.
    """
    out: list[str] = []
    values = iter(params)
    closing: str | None = None
    for char in sql:
        if closing is not None:
            closing = None if char == closing else closing
            out.append(char)
        elif char in "'\"":
            closing = char
            out.append(char)
        elif char == "?":
            try:
                out.append(sql_value(next(values)))
            except StopIteration:
                missing = "More ? marks than parameters"  # i18n: skip - a caller bug
                raise ValueError(missing) from None
        else:
            out.append(char)
    if next(values, _END) is not _END:
        raise ValueError("More parameters than ? marks")  # i18n: skip - a caller bug
    return "".join(out)
