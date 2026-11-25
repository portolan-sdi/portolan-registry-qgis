"""In-memory DuckDB connections for the tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def connect() -> DuckDBPyConnection:
    """Open an in-memory DuckDB connection that uses one thread."""
    import duckdb

    return duckdb.connect(config={"threads": 1})
