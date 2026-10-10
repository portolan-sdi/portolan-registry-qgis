"""Fixtures for tests that need PyQGIS.

The whole directory skips where ``qgis`` cannot be imported, which is the uv
environment. CI runs it in the QGIS container (.github/workflows/qgis.yml).

``catalog`` builds a small Portolan catalog with GDAL and serves it over
HTTP with range support, so every network path the plugin takes runs for
real against loopback. ``catalog["requests"]`` logs each request as
``(method, path)``.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import shutil
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("qgis.core")

from osgeo import gdal, ogr, osr  # noqa: E402
from qgis.core import QgsApplication, QgsProject  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
gdal.UseExceptions()

_RANGE = re.compile(r"bytes=(\d+)-(\d*)")


class RangeHandler(SimpleHTTPRequestHandler):
    """Static files with single-range support, which GDAL's /vsicurl/ needs."""

    def send_head(self):  # noqa: D102
        header = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if header is None or not path.is_file():
            return super().send_head()
        match = _RANGE.fullmatch(header.strip())
        size = path.stat().st_size
        if match is None:
            self.send_error(416)
            return None
        start = int(match.group(1))
        end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
        if start >= size:
            self.send_error(416)
            return None
        handle = path.open("rb")
        handle.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._remaining = end - start + 1
        return handle

    def copyfile(self, source, outputfile):  # noqa: D102
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            super().copyfile(source, outputfile)
            return
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self._remaining = None

    def log_message(self, format, *args):  # noqa: A002, D102
        pass

    def log_request(self, code="-", size="-"):  # noqa: D102
        # The tests read this log to count the requests GDAL sends.
        requests = getattr(self.server, "requests", None)
        if requests is not None:
            requests.append((self.command, self.path))


@pytest.fixture(scope="session", autouse=True)
def qgis_app():
    app = QgsApplication([], False)
    app.initQgis()
    yield app
    # Widgets that outlive the application crash the final garbage
    # collection, so collect them while Qt still exists.
    QgsProject.instance().clear()
    gc.collect()
    app.processEvents()
    app.exitQgis()


def wait_for(predicate, timeout=30.0):
    """Process Qt events until ``predicate()`` is true."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("Condition not met in time")
        QgsApplication.processEvents()
        time.sleep(0.01)


def _points(path: Path) -> None:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    driver = ogr.GetDriverByName("GeoJSON")
    source = driver.CreateDataSource(str(path))
    layer = source.CreateLayer("points", srs, ogr.wkbPoint)
    layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    for i in range(200):
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("name", f"p{i}")
        feature.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({11 + i * 0.01} {44 + i * 0.005})"))
        layer.CreateFeature(feature)
    source = None


def _raster(path: Path) -> None:
    memory = gdal.GetDriverByName("MEM").Create("", 64, 64, 1, gdal.GDT_Byte)
    memory.SetGeoTransform((11.0, 0.01, 0, 45.0, 0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    memory.SetProjection(srs.ExportToWkt())
    memory.GetRasterBand(1).Fill(120)
    gdal.Translate(str(path), memory, format="COG")


def _parquet(path: Path) -> None:
    """Write 200 points as GeoParquet with a bbox covering column, as DuckDB does.

    The date and timestamp columns guard against the memory-layer bug that
    loaded such files with no features.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(
        f"""COPY (SELECT i AS id, 'p' || i AS name, i * 0.5 AS score,
            DATE '2026-01-01' + i::INT AS day,
            TIMESTAMP '2026-01-01 03:00:00' + to_hours(i::BIGINT) AS seen,
            ST_Point(11 + i * 0.01, 44 + i * 0.005) AS geometry,
            {{'xmin': 11 + i * 0.01, 'ymin': 44 + i * 0.005,
              'xmax': 11 + i * 0.01, 'ymax': 44 + i * 0.005}} AS bbox
            FROM range(200) t(i))
        TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 20)"""
    )
    con.close()


def _images(root: Path, collection: Path) -> None:
    """Write a logo, a Lucide-style icon drawn in currentColor, and a thumbnail."""
    from qgis.PyQt.QtGui import QColor, QImage

    for path, size, color in (
        (root / "logo.png", (120, 40), "#2f7d63"),
        (collection / "thumbnail.png", (320, 200), "#4163cc"),
    ):
        image = QImage(*size, QImage.Format.Format_ARGB32)
        image.fill(QColor(color))
        assert image.save(str(path))
    (root / "leaf.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        '<circle cx="12" cy="12" r="9"/></svg>'
    )


def _multihash(path: Path) -> str:
    return "1220" + hashlib.sha256(path.read_bytes()).hexdigest()


def _asset(path: Path, href: str, media: str, roles: list[str]) -> dict:
    return {
        "href": href,
        "type": media,
        "roles": roles,
        "file:size": path.stat().st_size,
        "file:checksum": _multihash(path),
    }


def _build(root: Path) -> dict:
    collection = root / "points"
    (collection / "styles").mkdir(parents=True)
    geojson = collection / "points.geojson"
    _points(geojson)
    gdal.VectorTranslate(
        str(root / "points.pmtiles"),
        str(geojson),
        format="PMTiles",
        datasetCreationOptions=["MINZOOM=0", "MAXZOOM=8", "NAME=points"],
        layerName="points",
    )
    shutil.copy(root / "points.pmtiles", root / "other.pmtiles")
    _raster(collection / "relief.tif")
    _parquet(root / "points.parquet")
    _images(root, collection)
    style = {
        "version": 8,
        "name": "Points in red",
        "sources": {"data": {"type": "vector", "url": "pmtiles://../../points.pmtiles"}},
        "layers": [
            {
                "id": "points",
                "type": "circle",
                "source": "data",
                "source-layer": "points",
                "paint": {"circle-color": "#ff0000", "circle-radius": 6},
            }
        ],
    }
    (collection / "styles" / "red.json").write_text(json.dumps(style))
    # The second style names its archive as a bare relative path, the form
    # the specification's prose shows, with no pmtiles:// scheme.
    blue = {
        **style,
        "name": "Points in blue",
        "sources": {"data": {"type": "vector", "url": "../../points.pmtiles"}},
        "layers": [
            {**style["layers"][0], "paint": {"circle-color": "#0000ff", "circle-radius": 6}}
        ],
    }
    (collection / "styles" / "blue.json").write_text(json.dumps(blue))
    assets = {
        "geojson": _asset(geojson, "./points.geojson", "application/geo+json", ["data"]),
        "relief": _asset(
            collection / "relief.tif",
            "./relief.tif",
            "image/tiff; application=geotiff; profile=cloud-optimized",
            ["data"],
        ),
        "visual": _asset(
            root / "points.pmtiles", "../points.pmtiles", "application/vnd.pmtiles", ["visual"]
        ),
        "style-red": _asset(
            collection / "styles" / "red.json",
            "./styles/red.json",
            "application/vnd.mapbox.style+json",
            ["style", "default"],
        ),
        "style-blue": _asset(
            collection / "styles" / "blue.json",
            "./styles/blue.json",
            "application/vnd.mapbox.style+json",
            ["style"],
        ),
    }
    assets["thumbnail"] = _asset(
        collection / "thumbnail.png", "./thumbnail.png", "image/png", ["thumbnail"]
    )
    assets["data"] = _asset(
        root / "points.parquet", "../points.parquet", "application/vnd.apache.parquet", ["data"]
    )
    (collection / "collection.json").write_text(
        json.dumps(
            {
                "type": "Collection",
                "stac_version": "1.1.0",
                "id": "points",
                "title": "Test points",
                "description": "Points along a line near Bologna.",
                "license": "CC-BY-4.0",
                "extent": {
                    "spatial": {"bbox": [[11.0, 44.0, 13.0, 45.0]]},
                    "temporal": {"interval": [[None, None]]},
                },
                "links": [
                    {"rel": "root", "href": "../catalog.json"},
                    {"rel": "icon", "href": "../leaf.svg", "type": "image/svg+xml"},
                    {
                        "rel": "pmtiles",
                        "href": "../points.pmtiles",
                        "type": "application/vnd.pmtiles",
                        "title": "Point tiles",
                        "pmtiles:layers": ["points"],
                    },
                ],
                "assets": assets,
            }
        )
    )
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "type": "Catalog",
                "stac_version": "1.1.0",
                "id": "test",
                "title": "Test catalog",
                "description": "A catalog the tests build.",
                "links": [
                    {"rel": "root", "href": "./catalog.json"},
                    {"rel": "child", "href": "./points/collection.json", "title": "Test points"},
                    {"rel": "child", "href": "./missing/collection.json", "title": "Missing"},
                ],
            }
        )
    )
    return {}


@pytest.fixture(scope="session")
def catalog(tmp_path_factory):
    """Serve a built catalog. Yields a dict with ``url``, ``base``, and ``root``."""
    root = tmp_path_factory.mktemp("catalog")
    info = _build(root)

    def handler(*args, **kwargs):
        return RangeHandler(*args, directory=str(root), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    registry = {
        "type": "Catalog",
        "id": "portolan-registry",
        "links": [
            {
                "rel": "child",
                "href": f"{base}/catalog.json",
                "title": "Test catalog",
                "bbox": [11.0, 44.0, 13.0, 45.0],
                "portolan_registry:id": "test",
                "portolan_registry:status": "valid",
                "portolan_registry:collection_count": 1,
                "portolan_registry:total_size_bytes": 123456,
                "portolan_registry:licenses": {"CC-BY-4.0": 1},
                "portolan_registry:logo": {"href": f"{base}/logo.png", "type": "image/png"},
            }
        ],
    }
    (root / "registry.json").write_text(json.dumps(registry))
    yield {
        "url": f"{base}/catalog.json",
        "base": base,
        "root": root,
        "requests": server.requests,
        **info,
    }
    server.shutdown()
    server.server_close()
