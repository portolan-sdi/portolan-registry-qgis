#!/usr/bin/env python3
"""Time a pan across a remote PMTiles archive through the plugin's tile server.

Run it with the Python that has PyQGIS:

    QT_QPA_PLATFORM=offscreen /usr/bin/python3 scripts/bench_tiles.py

The script renders six screens east of Center City, Philadelphia, at about
zoom 15, then pans over the same screens a second time. It prints the render
time, the remote reads, and the loopback connections for each step. The last
line gives the time of the first visit and of the revisit apart. The revisit
draws from the QGIS cache. The first visit gains from kept connections, and
from the cache where neighboring screens share a tile.

``--baseline`` restores the transport of version 0.1.1: HTTP/1.0 and no
caching. Compare the two runs to see what keep-alive and caching save. The
regression tests in tests/in_qgis/test_tile_transport.py count the same
connections and reads against loopback, so they hold on any machine.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_URL = (
    "https://data.source.coop/nlebovits/phl-housing-demo/"
    "li_building_footprints/li_building_footprints.pmtiles"
)


def main() -> int:
    """Run the benchmark and print one line per step and a total."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="PMTiles archive URL")
    parser.add_argument("--baseline", action="store_true", help="use HTTP/1.0 and no caching")
    parser.add_argument("--steps", type=int, default=6, help="screens in one pass")
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # A fresh cache directory, so tiles from an earlier run do not count.
    os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="bench-tiles-")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from qgis.core import QgsApplication

    app = QgsApplication([], False)
    app.initQgis()
    try:
        _run(args.url, args.baseline, args.steps)
    finally:
        app.exitQgis()
    return 0


def _run(url: str, baseline: bool, steps: int) -> None:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsMapRendererParallelJob,
        QgsMapSettings,
        QgsProject,
        QgsRectangle,
    )
    from qgis.PyQt.QtCore import QSize

    from portolan_registry_qgis.qgis_io import layers, network, tileserver

    if baseline:
        tileserver._Handler.protocol_version = "HTTP/1.0"
        tileserver.CACHE_CONTROL = "no-store"

    counts = {"reads": 0, "bytes": 0, "connections": 0}
    lock = threading.Lock()
    fetch = network._range
    setup = tileserver._Handler.setup

    def counted_fetch(url: str, offset: int, length: int) -> tuple[bytes, bytes]:
        body, cache_control = fetch(url, offset, length)
        with lock:
            counts["reads"] += 1
            counts["bytes"] += len(body)
        return body, cache_control

    def counted_setup(handler: Any) -> None:
        with lock:
            counts["connections"] += 1
        setup(handler)

    network._range = counted_fetch
    tileserver._Handler.setup = counted_setup  # type: ignore[method-assign,assignment]

    server = tileserver.TileServer()
    layer = layers.vector_tile_layer(server, tileserver.open_archive(url), "bench")
    merc = QgsCoordinateReferenceSystem("EPSG:3857")
    to_merc = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"), merc, QgsProject.instance()
    )
    mode = "baseline" if baseline else "current"
    totals = {1: 0.0, 2: 0.0}
    for sweep in (1, 2):
        for step in range(steps):
            west = -75.20 + step * 0.015
            settings = QgsMapSettings()
            settings.setLayers([layer])
            settings.setDestinationCrs(merc)
            settings.setExtent(
                to_merc.transformBoundingBox(QgsRectangle(west, 39.945, west + 0.015, 39.955))
            )
            settings.setOutputSize(QSize(1600, 1000))
            for key in counts:
                counts[key] = 0
            job = QgsMapRendererParallelJob(settings)
            start = time.perf_counter()
            job.start()
            job.waitForFinished()
            took = time.perf_counter() - start
            totals[sweep] += took
            print(
                f"{mode} pass {sweep} step {step}: {took:5.2f} s, "
                f"{counts['reads']:2} remote reads, {counts['bytes'] // 1000:5} kB, "
                f"{counts['connections']:2} new loopback connections"
            )
    print(
        f"{mode} first visit: {totals[1]:.2f} s, revisit: {totals[2]:.2f} s, "
        f"total: {totals[1] + totals[2]:.2f} s"
    )
    server.stop()


if __name__ == "__main__":
    sys.exit(main())
