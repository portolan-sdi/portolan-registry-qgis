"""The GeoParquet points file that the core and the in-QGIS tests both read."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def write_points(con, path: Path, columns: str) -> None:
    """Write 200 points near Bologna as GeoParquet with a bbox covering column.

    ``con`` is a DuckDB connection with the spatial extension loaded.
    ``columns`` is the SQL for the attribute columns that follow ``id``. The
    file has row groups of 20 rows, so a filtered read can skip groups.
    """
    con.execute(
        f"""COPY (SELECT i AS id, {columns},
            ST_Point(11 + i * 0.01, 44 + i * 0.005) AS geometry,
            {{'xmin': 11 + i * 0.01, 'ymin': 44 + i * 0.005,
              'xmax': 11 + i * 0.01, 'ymax': 44 + i * 0.005}} AS bbox
            FROM range(200) t(i))
        TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 20)"""
    )
