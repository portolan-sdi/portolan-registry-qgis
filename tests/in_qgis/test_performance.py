"""Guard the speed of GeoParquet loads against a return of per-feature Python work.

The test times a load of 200,000 polygons against a reference: DuckDB reading
the same rows into Python. Both run on the same machine, so the ratio holds
on a slow CI runner and on a fast laptop. The memory-layer loader that the
GeoPackage copy replaced took about 10 times the reference. The copy takes
about 1.1 times.

``build`` runs on the main thread, where QGIS stops responding while it
works, so it also has an absolute limit.
"""

from __future__ import annotations

import time

import pytest
from qgis.core import QgsProject

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.qgis_io import parquet_layer

FEATURES = 200_000
# The memory-layer loader measured about 10. The copy measures about 1.1.
MAX_RATIO = 4.0
MAX_BUILD_SECONDS = 0.5
RUNS = 3


@pytest.fixture(scope="module")
def polygons(tmp_path_factory):
    """Write 200,000 nine-vertex polygons with int, text, double, and date columns."""
    path = tmp_path_factory.mktemp("bench") / "polygons.parquet"
    con = parquet_query.connection()
    con.execute(
        f"""COPY (SELECT i AS id, 'name_' || i AS name, i * 0.5 AS value,
            DATE '2020-01-01' + (i % 1000)::INT AS day,
            ST_Buffer(ST_Point(-75 + (i % 1000) * 0.001, 40 + (i // 1000) * 0.001),
                      0.0004, 2) AS geometry
            FROM range({FEATURES}) t(i))
        TO '{path}' (FORMAT parquet)"""
    )
    return str(path)


def _best(function):
    times = []
    for _ in range(RUNS):
        start = time.perf_counter()
        function()
        times.append(time.perf_counter() - start)
    return min(times)


def test_load_speed(polygons):
    con = parquet_query.connection()
    context = QgsProject.instance().transformContext()

    def reference():
        rows = con.execute(
            "SELECT * REPLACE (ST_AsWKB(geometry) AS geometry) FROM read_parquet(?)",
            [polygons],
        ).fetchall()
        assert len(rows) == FEATURES

    prepared = []

    def load():
        planned = parquet_layer.plan(polygons, context)
        prepared.append(parquet_layer.prepare(planned))

    builds = []

    def build():
        layer, _ = parquet_layer.build(prepared[-1], "bench")
        builds.append(layer)

    reference_seconds = _best(reference)
    load_seconds = _best(load)
    build_seconds = _best(build)
    assert builds[-1].featureCount() == FEATURES
    for item in prepared:
        parquet_layer.discard_path(item.path)

    ratio = load_seconds / reference_seconds
    report = (
        f"reference {reference_seconds:.2f} s, load {load_seconds:.2f} s "
        f"({ratio:.1f}x), build {build_seconds:.3f} s"
    )
    print(report)
    assert ratio <= MAX_RATIO, report
    assert build_seconds <= MAX_BUILD_SECONDS, report
