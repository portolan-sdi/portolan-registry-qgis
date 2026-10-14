"""Checks of the core read path against published catalogs.

Deselected by default. Run with ``-m network``.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from portolan_registry_qgis.core import parquet_query, style
from portolan_registry_qgis.core.loadable import is_readable, pick_targets
from portolan_registry_qgis.core.stac import read_document

pytestmark = pytest.mark.network
pytest.importorskip("duckdb")

PARTITIONED = [
    # Relative glob on the data asset, partitions as yearly folders.
    "https://data.source.coop/cholmes/portolan-nl/rvo/brp_gewaspercelen/collection.json",
    # Absolute HTTPS glob on the asset, relative partition:glob, hive keys.
    "https://data.source.coop/ftw/global-data-2e/vector/2020/collection.json",
]


def _read(url):
    request = urllib.request.Request(url, headers={"User-Agent": "portolan-registry-qgis-tests"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


@pytest.mark.parametrize("url", PARTITIONED)
def test_live_partitioned_collection_loads_through_its_bucket(url):
    document = read_document(json.loads(_read(url)), url)
    load = pick_targets(document.loadable(), duckdb_ok=True).load
    assert load is not None
    assert load.asset is not None
    assert is_readable(load)
    assert load.asset.read_url.startswith("s3://us-west-2.opendata.source.coop/")
    con = parquet_query.connection()
    plan = parquet_query.read_plan(con, load.asset.read_url)
    assert plan.geometry is not None
    assert len(parquet_query.list_files(con, load.asset.read_url)) > 1


def test_live_overture_tile_template_style_names_its_archive():
    url = "https://nlebovits.github.io/overture-portolan/base/land/collection.json"
    document = read_document(json.loads(_read(url)), url)
    default = document.default_style
    assert default is not None
    sources = style.pmtiles_sources(style.parse_style(_read(default.href)), default.href)
    archives = style.archive_urls(document)
    assert sources
    assert all(style.match_archive(source, archives) for source in sources.values())
