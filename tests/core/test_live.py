"""Checks of the core read path against published catalogs.

Deselected by default. Run with ``-m network``.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from portolan_registry_qgis.core import parquet_query, style
from portolan_registry_qgis.core.download import WALK_WORKERS, Walk, walk
from portolan_registry_qgis.core.loadable import is_readable, pick_targets, reads_items
from portolan_registry_qgis.core.mirror import item_href
from portolan_registry_qgis.core.stac import read_document

pytestmark = pytest.mark.network
pytest.importorskip("duckdb")

PARTITIONED = [
    # Relative glob on the data asset, partitions as yearly folders.
    "https://data.source.coop/cholmes/portolan-nl/rvo/brp_gewaspercelen/collection.json",
    # Absolute HTTPS glob on the asset, relative partition:glob, hive keys.
    "https://data.source.coop/ftw/global-data-2e/vector/2020/collection.json",
    # Absolute HTTPS partition:glob, items in the collection and in child catalogs.
    "https://data.source.coop/ftw/global-data/predictions/vectors/collection.json",
]


def _read(url):
    # Item names such as Åland_Islands.json need percent-encoding in the request line.
    quoted = urllib.parse.quote(url, safe=":/?&=%@+,;~")
    request = urllib.request.Request(quoted, headers={"User-Agent": "portolan-registry-qgis-tests"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


@pytest.mark.parametrize("url", PARTITIONED)
def test_live_partitioned_collection_loads_through_its_items(url):
    document = read_document(json.loads(_read(url)), url)
    load = pick_targets(document.loadable(), duckdb_ok=True).load
    assert load is not None
    assert reads_items(document, load)
    assert is_readable(load, document)
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(WALK_WORKERS) as executor:
        walked = list(
            walk(lambda href: json.loads(_read(href)), url, failures, Walk(20000, None, executor))
        )
    hrefs = [item_href(doc.assets) for doc in walked if doc.kind == "item"]
    files = [href for href in hrefs if href is not None]
    assert failures == []
    assert len(files) > 1
    plan = parquet_query.read_plan(parquet_query.connection(), files[:3])
    assert plan.geometry is not None


def test_live_overture_tile_template_style_names_its_archive():
    url = "https://nlebovits.github.io/overture-portolan/base/land/collection.json"
    document = read_document(json.loads(_read(url)), url)
    default = document.default_style
    assert default is not None
    sources = style.pmtiles_sources(style.parse_style(_read(default.href)), default.href)
    archives = style.archive_urls(document)
    assert sources
    assert all(style.match_archive(source, archives) for source in sources.values())
