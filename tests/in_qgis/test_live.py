"""Checks against the live registry and a live catalog.

Deselected by default. Run with ``-m network``.
"""

from __future__ import annotations

import pytest
from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsRectangle, QgsWkbTypes
from qgis.PyQt.QtGui import QImage

from portolan_registry_qgis.core.registry import REGISTRY_URL, parse_registry
from portolan_registry_qgis.core.stac import read_document
from portolan_registry_qgis.qgis_io import layers, parquet_layer
from portolan_registry_qgis.qgis_io.network import fetch_bytes, fetch_json
from portolan_registry_qgis.qgis_io.tileserver import TileServer, open_archive, read_tile

from .test_io import _render

pytestmark = pytest.mark.network


def test_live_registry_parses():
    entries = parse_registry(fetch_json(REGISTRY_URL))
    assert len(entries) > 50
    assert all(entry.url.startswith("https://") for entry in entries)
    assert any(entry.id == "anncsu" for entry in entries)


def test_live_anncsu_tiles_render_with_their_style():
    (anncsu,) = (e for e in parse_registry(fetch_json(REGISTRY_URL)) if e.id == "anncsu")
    catalog = read_document(fetch_json(anncsu.url), anncsu.url)
    href = next(n.href for n in catalog.children if n.href.endswith("/indirizzi/collection.json"))
    collection = read_document(fetch_json(href), href)
    server = TileServer()
    try:
        urls = layers.archive_urls(collection)
        prepared = layers.prepare_pmtiles(collection, urls, fetch_bytes)
        built, warnings = layers.build_pmtiles(server, prepared, QImage.fromData)
        (layer,) = built
        assert layer.isValid()
        # Web Mercator extent over Rome. The style draws addresses in #0066cc.
        image = _render(layer, QgsRectangle(1_380_000, 5_130_000, 1_400_000, 5_150_000))
        blue = sum(
            1
            for x in range(0, image.width(), 2)
            for y in range(0, image.height(), 2)
            if image.pixelColor(x, y).blue() > 150 and image.pixelColor(x, y).red() < 80
        )
        assert blue > 0, warnings
    finally:
        server.stop()


PHL = "https://data.source.coop/nlebovits/phl-housing-demo/land_use"


def test_live_source_coop_pmtiles():
    """The archive that failed through GDAL's /vsipmtiles/ in a Flatpak QGIS 3.40."""
    archive = open_archive(f"{PHL}/land_use.pmtiles")
    assert archive.vector_layers == ("land_use",)
    assert archive.max_zoom == 14
    assert read_tile(archive, 10, 298, 387)


def test_live_source_coop_geoparquet_in_an_extent():
    """The file that failed through OGR. DuckDB reads one block of Center City."""
    context = QgsProject.instance().transformContext()
    prepared = parquet_layer.prepare(
        f"{PHL}/land_use.parquet",
        context,
        QgsRectangle(-75.17, 39.94, -75.15, 39.96),
        QgsCoordinateReferenceSystem("EPSG:4326"),
    )
    layer, refused = parquet_layer.build(prepared, "land_use")
    assert refused == 0
    assert layer.featureCount() == 7332
    assert layer.wkbType() == QgsWkbTypes.Type.MultiPolygon
    assert "c_dig1desc" in layer.fields().names()
