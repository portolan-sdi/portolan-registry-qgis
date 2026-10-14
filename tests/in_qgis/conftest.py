"""Fixtures for tests that need PyQGIS. The directory skips where qgis is absent.

``catalog`` serves a small Portolan catalog over loopback HTTP with range
support. ``catalog["requests"]`` logs each request as ``(method, path)``.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import shutil
import time
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

import pytest

pytest.importorskip("qgis.core")

from osgeo import gdal, ogr, osr
from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsProject
from qgis.gui import QgsMapCanvas, QgsMessageBar
from qgis.PyQt.QtWidgets import QMainWindow

from tests.loopback import base_url, serve
from tests.points import write_points
from tests.stac_builders import box_polygon, make_catalog, make_collection, make_item

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
gdal.UseExceptions()

_RANGE = re.compile(r"bytes=(\d+)-(\d*)")


class RangeHandler(SimpleHTTPRequestHandler):
    """Static files with single-range support, which GDAL's /vsicurl/ needs."""

    def send_head(self):
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

    def copyfile(self, source, outputfile):
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

    def log_message(self, format, *args):
        pass

    def log_request(self, code="-", size="-"):
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


class FakeIface:
    """The parts of QgisInterface the plugin calls."""

    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.canvas.resize(400, 400)
        self.bar = QgsMessageBar()
        self.menu = []
        self.toolbar = []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def addPluginToWebMenu(self, _menu, action):
        self.menu.append(action)

    def removePluginWebMenu(self, _menu, action):
        self.menu.remove(action)

    def addWebToolBarIcon(self, action):
        self.toolbar.append(action)

    def removeWebToolBarIcon(self, action):
        self.toolbar.remove(action)


@pytest.fixture
def iface():
    fake = FakeIface()
    yield fake
    QgsProject.instance().clear()
    for widget in (fake.canvas, fake.bar, fake.window):
        widget.deleteLater()
    QgsApplication.processEvents()


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


def _raster(path: Path, west: float = 11.0, north: float = 45.0, value: int = 120) -> None:
    memory = gdal.GetDriverByName("MEM").Create("", 64, 64, 1, gdal.GDT_Byte)
    memory.SetGeoTransform((west, 0.01, 0, north, 0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    memory.SetProjection(srs.ExportToWkt())
    memory.GetRasterBand(1).Fill(value)
    gdal.Translate(str(path), memory, format="COG")


# Two COG items, 0.64 degrees square, side by side along the parallel 44.
TILES = {"a": (11.0, 44.64), "b": (12.0, 44.64)}


def _tiles(root: Path) -> None:
    """Write a collection of two COG items and its stac-geoparquet mirror.

    The root catalog does not link the collection. Item ``a`` names its COG
    relative to the mirror, and item ``b`` relative to its ``self`` link.
    """
    import duckdb

    folder = root / "tiles"
    rows = []
    for index, (key, (west, north)) in enumerate(TILES.items()):
        (folder / key).mkdir(parents=True)
        _raster(folder / key / f"{key}.tif", west, north, 50 + index * 100)
        bbox = [west, north - 0.64, west + 0.64, north]
        item = make_item(
            key,
            bbox,
            [{"rel": "collection", "href": "../collection.json"}],
            geometry=box_polygon(bbox),
            properties={"datetime": "2026-01-01T00:00:00Z"},
            assets={
                "thumbnail": {
                    "href": "../../logo.png",
                    "type": "image/png",
                    "roles": ["thumbnail"],
                },
                "image": {
                    "href": f"./{key}.tif",
                    "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                    "roles": ["data"],
                },
            },
        )
        (folder / key / f"{key}.json").write_text(json.dumps(item))
        # An asset href is relative to its item document. Item a has no self
        # link, as in the GHSL mirror, so its document comes from the
        # collection's item link with the same ID.
        href, links = (f"./{key}.tif", "[]") if key == "a" else (f"./{key}.tif", None)
        if links is None:
            links = f"[{{'rel': 'self', 'href': './{key}/{key}.json'}}]"
        rows.append(
            f"SELECT '{key}' AS id, "
            f"{{'xmin': {bbox[0]}, 'ymin': {bbox[1]}, 'xmax': {bbox[2]}, 'ymax': {bbox[3]}}} AS bbox, "
            f"ST_MakeEnvelope({bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}) AS geometry, "
            f"{{'thumbnail': {{'href': '../logo.png', 'type': 'image/png', 'roles': ['thumbnail']}}, "
            f"'image': {{'href': '{href}', 'type': 'image/tiff; application=geotiff', "
            f"'roles': ['data']}}}} AS assets, "
            f"{links}::STRUCT(rel VARCHAR, href VARCHAR)[] AS links"
        )
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(
        f"COPY ({' UNION ALL '.join(rows)}) TO '{folder / 'items.parquet'}' (FORMAT parquet)"
    )
    con.close()
    collection = make_collection(
        "tiles",
        [f"./{key}/{key}.json" for key in TILES],
        title="Test tiles",
        description="Two COG items.",
        license="CC-BY-4.0",
        extent={
            "spatial": {"bbox": [[11.0, 44.0, 12.64, 44.64]]},
            "temporal": {"interval": [[None, None]]},
        },
        assets={
            "mirror": {
                "href": "./items.parquet",
                "type": "application/vnd.apache.parquet",
                "roles": ["collection-mirror"],
            }
        },
    )
    (folder / "collection.json").write_text(json.dumps(collection))


def _tables(root: Path) -> None:
    """Write CSV files with and without coordinates, and Parquet with no geometry."""
    import duckdb

    folder = root / "table"
    folder.mkdir()
    lines = ["id,Name,Longitude,LAT"]
    lines += [f"{i},p{i},{11 + i * 0.01},{44 + i * 0.005}" for i in range(20)]
    (folder / "points.csv").write_text("\n".join(lines) + "\n")
    (folder / "plain.csv").write_text("id;name;score\n1;a;1.5\n2;b;2.5\n3;c;3.5\n")
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT i AS id, 'r' || i AS name, i * 2.5 AS score FROM range(30) t(i)) "
        f"TO '{folder / 'plain.parquet'}' (FORMAT parquet)"
    )
    con.close()


def _parquet(path: Path) -> None:
    """Write 200 points as GeoParquet with a bbox covering column, as DuckDB does.

    The date and timestamp columns guard against the memory-layer bug that
    loaded such files with no features.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    write_points(
        con,
        path,
        "'p' || i AS name, i * 0.5 AS score, DATE '2026-01-01' + i::INT AS day, "
        "TIMESTAMP '2026-01-01 03:00:00' + to_hours(i::BIGINT) AS seen",
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
    _tiles(root)
    _tables(root)
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
    links = [
        {"rel": "root", "href": "../catalog.json"},
        {"rel": "icon", "href": "../leaf.svg", "type": "image/svg+xml"},
        {
            "rel": "pmtiles",
            "href": "../points.pmtiles",
            "type": "application/vnd.pmtiles",
            "title": "Point tiles",
            "pmtiles:layers": ["points"],
        },
    ]
    points = make_collection(
        "points",
        other_links=links,
        title="Test points",
        description="Points along a line near Bologna.",
        license="CC-BY-4.0",
        extent={
            "spatial": {"bbox": [[11.0, 44.0, 13.0, 45.0]]},
            "temporal": {"interval": [[None, None]]},
        },
        assets=assets,
    )
    (collection / "collection.json").write_text(json.dumps(points))
    children = [
        {"rel": "root", "href": "./catalog.json"},
        {"rel": "child", "href": "./points/collection.json", "title": "Test points"},
        {"rel": "child", "href": "./missing/collection.json", "title": "Missing"},
    ]
    test_catalog = make_catalog(
        "test",
        other_links=children,
        title="Test catalog",
        description="A catalog the tests build.",
    )
    (root / "catalog.json").write_text(json.dumps(test_catalog))
    return {}


def _registries(root: Path, base: str) -> None:
    """Write the registry of the test catalog, and a second one for the mosaic tests."""
    entry = {
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
    (root / "registry.json").write_text(
        json.dumps(make_catalog("portolan-registry", other_links=[entry]))
    )
    # A second registry lists a catalog of the tiles collection alone, for
    # the mosaic tests. The first registry stays as the other tests expect.
    tiles_catalog = make_catalog(
        "tiles-catalog",
        other_links=[{"rel": "child", "href": "./tiles/collection.json", "title": "Test tiles"}],
        title="Tiles catalog",
        description="A catalog of COG items.",
    )
    (root / "tiles-catalog.json").write_text(json.dumps(tiles_catalog))
    tiles_entry = {
        **entry,
        "href": f"{base}/tiles-catalog.json",
        "title": "Tiles catalog",
        "portolan_registry:id": "tiles",
    }
    (root / "tiles-registry.json").write_text(
        json.dumps(make_catalog("portolan-registry", other_links=[tiles_entry]))
    )


@pytest.fixture(scope="session")
def catalog(tmp_path_factory):
    """Serve a built catalog. Yields a dict with ``url``, ``base``, and ``root``."""
    root = tmp_path_factory.mktemp("catalog")
    info = _build(root)

    def handler(*args, **kwargs):
        return RangeHandler(*args, directory=str(root), **kwargs)

    with serve(handler) as server:
        server.requests = []
        base = base_url(server)
        _registries(root, base)
        yield {
            "url": f"{base}/catalog.json",
            "base": base,
            "root": root,
            "requests": server.requests,
            **info,
        }
