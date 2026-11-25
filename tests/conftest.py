"""Hooks for every test in the suite."""

from __future__ import annotations

import contextlib

from portolan_registry_qgis.core import parquet_query


def pytest_configure(config):
    """Install the DuckDB extensions once, before the xdist workers start."""
    if hasattr(config, "workerinput") or not parquet_query.duckdb_status()[0]:
        return
    import duckdb

    with contextlib.suppress(duckdb.Error):
        parquet_query.connection()
    parquet_query.close()
