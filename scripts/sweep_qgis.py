#!/usr/bin/env python3
"""Add each registered collection's view and load targets as QGIS layers, and report what fails.

Run it with the Python that has PyQGIS. The walk reuses ``sweep_registry``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sweep_registry import ROOT, Result, Sweep, fetch, markdown

sys.path.insert(0, str(ROOT))

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor, QImage

from portolan_registry_qgis.core import registry
from portolan_registry_qgis.core.loadable import pick_targets
from portolan_registry_qgis.core.stac import Document, read_document
from portolan_registry_qgis.core.style import archive_urls
from portolan_registry_qgis.qgis_io import layers
from portolan_registry_qgis.qgis_io.network import fetch_bytes, fetch_json
from portolan_registry_qgis.qgis_io.tileserver import TileServer

WORLD = 40_075_016.686
MERCATOR = QgsCoordinateReferenceSystem("EPSG:3857")
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def collections(entry: registry.CatalogEntry, max_documents: int) -> list[str]:
    """Return the collection URLs of one catalog, walked without QGIS."""
    sweep = Sweep(entry.id, max_documents, None)
    return [doc.href for doc, _raw in sweep.walk(entry.url) if doc.kind == "collection"]


def render(layer: Any, extent: QgsRectangle) -> int:
    """Render ``layer`` over ``extent`` in Web Mercator and count the sampled non-white pixels."""
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(MERCATOR)
    settings.setOutputSize(QSize(256, 256))
    settings.setBackgroundColor(QColor(255, 255, 255))
    settings.setExtent(extent)
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    image: QImage = job.renderedImage()
    return sum(
        1
        for x in range(0, 256, 4)
        for y in range(0, 256, 4)
        if image.pixelColor(x, y).name() != "#ffffff"
    )


def fit_zoom(bounds: tuple[float, float, float, float]) -> int:
    """Return the zoom at which ``bounds`` fills about one 256-pixel tile."""
    span = max(bounds[2] - bounds[0], bounds[3] - bounds[1], 1e-6)
    return max(0, min(16, int(math.log2(360 / span))))


def view_extent(bounds: tuple[float, float, float, float], zoom: int) -> QgsRectangle:
    """Return the Web Mercator extent of ``bounds``, or one tile around its centre at ``zoom``."""
    transform = QgsCoordinateTransform(WGS84, MERCATOR, QgsProject.instance())
    south, north = max(bounds[1], -85.0), min(bounds[3], 85.0)
    if zoom <= fit_zoom(bounds):
        low = transform.transform(QgsPointXY(bounds[0], south))
        high = transform.transform(QgsPointXY(bounds[2], north))
        return QgsRectangle(low.x(), low.y(), high.x(), high.y())
    centre = transform.transform(QgsPointXY((bounds[0] + bounds[2]) / 2, (south + north) / 2))
    half = WORLD / 2 ** (zoom + 1)
    return QgsRectangle(centre.x() - half, centre.y() - half, centre.x() + half, centre.y() + half)


class QgisSweep(Sweep):
    """Collect QGIS layer results for one collection."""

    def __init__(self, catalog: str, server: TileServer) -> None:
        super().__init__(catalog, 0, None)
        self.server = server

    def collection(self, url: str) -> None:
        """Read one collection through the plugin's network code and add its targets."""
        holder: dict[str, Document] = {}

        def read() -> str:
            holder["doc"] = read_document(fetch_json(url), url)
            return ""

        self.record(url, "qgis read", read)
        document = holder.get("doc")
        if document is None:
            return
        view, load = pick_targets(document.loadable(), duckdb_ok=True)
        if view is not None and view.format == "pmtiles":
            self.record(url, "qgis pmtiles", lambda: self.pmtiles(document))
        elif view is not None and view.asset is not None:
            self.record(url, f"qgis view {view.format}", lambda: self.asset(view.asset, document))
        if load is not None and load.asset is not None and load.format != "parquet":
            self.record(url, f"qgis load {load.format}", lambda: self.asset(load.asset, document))

    def pmtiles(self, document: Document) -> str:
        """Build the PMTiles layers with their styles, and render one tile of each."""
        prepared = layers.prepare_pmtiles(document, archive_urls(document), fetch_bytes)
        built, warnings = layers.build_pmtiles(self.server, prepared, QImage.fromData)
        notes = []
        for layer, part in zip(built, prepared.parts, strict=True):
            if not layer.isValid():
                raise ValueError(f"invalid layer {part.title}")
            styles = len(layer.styleManager().styles()) - 1
            if document.styles and styles == 0:
                self.flag(
                    document.href, "qgis style", f"{part.archive.url}: no catalog style applied"
                )
            bounds = part.archive.bounds or (-180.0, -85.0, 180.0, 85.0)
            zoom = max(part.archive.min_zoom, min(part.archive.max_zoom, fit_zoom(bounds)))
            if render(layer, view_extent(bounds, zoom)) == 0:
                self.flag(document.href, "qgis blank", f"{part.archive.url} at z{zoom}")
            notes.append(f"{part.title}: {styles} styles")
        for warning in warnings:
            self.flag(document.href, "qgis warning", warning[:300])
        return "; ".join(notes)

    def asset(self, asset: Any, document: Document) -> str:
        """Open one data asset in place and render its centre."""
        layer = layers.asset_layer(asset)
        if not layer.isValid():
            raise ValueError(layer.error().summary() or "invalid layer")
        bbox = document.bbox or (-180.0, -85.0, 180.0, 85.0)
        zoom = fit_zoom(bbox)
        if render(layer, view_extent(bbox, zoom)) == 0:
            self.flag(document.href, "qgis blank", f"{asset.href} at z{zoom}")
        return str(layer.providerType())


def main(argv: list[str] | None = None) -> int:
    """Walk the registry, then add each collection's targets in QGIS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("sweep-qgis"))
    parser.add_argument("--only", nargs="*", default=None, help="catalog ids to sweep")
    parser.add_argument("--max-documents", type=int, default=300)
    args = parser.parse_args(argv)
    app = QgsApplication([], False)
    app.initQgis()
    body, _final, _headers = fetch(registry.REGISTRY_URL)
    entries = registry.parse_registry(json.loads(body))
    if args.only:
        entries = [entry for entry in entries if entry.id in args.only]
    with ThreadPoolExecutor(8) as pool:
        walked = list(pool.map(lambda e: collections(e, args.max_documents), entries))
    server = TileServer()
    results: list[Result] = []
    try:
        for entry, urls in zip(entries, walked, strict=True):
            sweep = QgisSweep(entry.id, server)
            for url in urls:
                sweep.collection(url)
            results.extend(sweep.results)
            failed = sum(not r.ok for r in sweep.results)
            print(f"{entry.id}: {len(urls)} collections, {failed} failures", file=sys.stderr)
    finally:
        server.stop()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "sweep.json").write_text(json.dumps([asdict(r) for r in results], indent=1))
    (args.out / "sweep.md").write_text(markdown(results))
    QgsProject.instance().clear()
    app.exitQgis()
    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
