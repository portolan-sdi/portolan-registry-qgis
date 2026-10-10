"""How tiles travel from a remote archive through the loopback server to QGIS.

The tests count connections and reads, not seconds, so they hold on any
machine. ``scripts/bench_tiles.py`` measures the time against a real host.
"""

from __future__ import annotations

import http.client
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeedback,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QSize

from portolan_registry_qgis.qgis_io import layers, tileserver
from portolan_registry_qgis.qgis_io.network import (
    NetworkError,
    cancel_past,
    fetch_range,
    range_fetcher,
)
from portolan_registry_qgis.qgis_io.tileserver import CACHE_CONTROL, TileServer, open_archive

from .conftest import RangeHandler


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def remote(catalog):
    """Serve the catalog with HTTP/1.1 keep-alive and count its connections."""
    connections = []

    class KeepAlive(RangeHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            connections.append(threading.get_ident())
            super().setup()

    def handler(*args, **kwargs):
        return KeepAlive(*args, directory=str(catalog["root"]), **kwargs)

    server = _serve(handler)
    yield f"http://127.0.0.1:{server.server_address[1]}", connections
    server.shutdown()
    server.server_close()


@pytest.fixture
def server():
    tiles = TileServer()
    yield tiles
    tiles.stop()


@pytest.fixture
def loopback(monkeypatch):
    """Count the loopback server's connections and the tiles it reads."""
    counts = {"connections": 0, "requests": 0, "tiles": 0}
    setup = tileserver._Handler.setup
    get = tileserver._Handler.do_GET
    read = tileserver.read_tile

    def counted_setup(self):
        counts["connections"] += 1
        setup(self)

    def counted_get(self):
        counts["requests"] += 1
        get(self)

    def counted_read(archive, z, x, y):
        tile = read(archive, z, x, y)
        counts["tiles"] += tile is not None
        return tile

    monkeypatch.setattr(tileserver._Handler, "setup", counted_setup)
    monkeypatch.setattr(tileserver._Handler, "do_GET", counted_get)
    monkeypatch.setattr(tileserver, "read_tile", counted_read)
    return counts


def _render(layer, zoom):
    """Render about two tiles across at ``zoom``, centered on the test points."""
    merc = QgsCoordinateReferenceSystem("EPSG:3857")
    center = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"), merc, QgsProject.instance()
    ).transform(12.0, 44.5)
    half = 40075016.0 / 2**zoom
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(merc)
    settings.setExtent(
        QgsRectangle(center.x() - half, center.y() - half, center.x() + half, center.y() + half)
    )
    settings.setOutputSize(QSize(512, 512))
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()


def test_a_connection_serves_many_tiles(catalog, server):
    template = server.register(open_archive(f"{catalog['base']}/points.pmtiles"))
    host, path = template.removeprefix("http://").split("/", 1)
    connection = http.client.HTTPConnection(host, timeout=10)
    sockets = set()
    for zxy, status in (("0/0/0", 200), ("8/0/0", 204), ("0/0/0", 200)):
        connection.request("GET", "/" + path.replace("{z}/{x}/{y}", zxy))
        response = connection.getresponse()
        response.read()
        assert response.status == status
        assert response.getheader("Cache-Control") == CACHE_CONTROL
        assert not response.will_close
        sockets.add(id(connection.sock))
    assert len(sockets) == 1
    # A kept connection outlives the listener, but it serves no more tiles.
    server.stop()
    connection.request("GET", "/" + path.replace("{z}/{x}/{y}", "0/0/0"))
    assert connection.getresponse().status == 404
    connection.close()


def test_qgis_keeps_connections_and_caches_tiles(remote, server, loopback):
    base, remote_connections = remote
    archive = open_archive(f"{base}/points.pmtiles")
    layer = layers.vector_tile_layer(server, archive, "points")
    remote_connections.clear()

    for zoom in range(9):
        _render(layer, zoom)
    first = dict(loopback)
    assert first["tiles"] > 6
    # QGIS keeps up to 6 connections open, so it does not open one per tile.
    assert first["connections"] <= 6 < first["requests"]
    # Each handler thread keeps its own connection to the remote host.
    assert len(remote_connections) <= first["connections"]

    for zoom in range(9):
        _render(layer, zoom)
    # Every tile of the second pass comes from QGIS's cache. Qt does not
    # cache a 204, so empty tiles still reach the server.
    assert loopback["tiles"] == first["tiles"]


def test_register_reuses_an_unchanged_archive(catalog, server, tmp_path):
    url = f"{catalog['base']}/points.pmtiles"
    first = open_archive(url)
    template = server.register(first)
    assert server.register(open_archive(url)) == template
    token = template.split("/")[3]
    assert server._server.archives[token] is first

    # A republished archive has a different header, here a lower max zoom.
    changed = bytearray((catalog["root"] / "points.pmtiles").read_bytes())
    changed[101] = 7
    (tmp_path / "points.pmtiles").write_bytes(bytes(changed))
    second = open_archive(url, range_fetcher(str(tmp_path / "points.pmtiles")))
    assert second.max_zoom == 7
    new_template = server.register(second)
    assert new_template != template
    assert server._server.archives[token] is second
    assert server._server.archives[new_template.split("/")[3]] is second


def test_fetch_range_rejects_a_server_that_ignores_ranges(catalog):
    def handler(*args, **kwargs):
        return SimpleHTTPRequestHandler(*args, directory=str(catalog["root"]), **kwargs)

    plain = _serve(handler)
    base = f"http://127.0.0.1:{plain.server_address[1]}"
    try:
        with pytest.raises(NetworkError, match="ignores HTTP range requests"):
            fetch_range(f"{base}/points.pmtiles", 0, 127)
        # A range past the end of a small file is rejected the same way.
        with pytest.raises(NetworkError, match="ignores HTTP range requests"):
            fetch_range(f"{base}/catalog.json", 0, 10**6)
    finally:
        plain.shutdown()
        plain.server_close()


def test_fetch_range_cancels_a_body_longer_than_the_range(tmp_path):
    """QGIS 3.34 and 3.36 read a 200 to the end. A 206 that sends the whole
    file takes the same path on every version, so this test covers the cancel.
    """
    (tmp_path / "big.bin").write_bytes(bytes(8 << 20))
    sent = []

    class WholeFile(SimpleHTTPRequestHandler):
        def send_response(self, code, message=None):
            super().send_response(206 if code == 200 else code, message)

        def copyfile(self, source, outputfile):
            try:
                while chunk := source.read(65536):
                    outputfile.write(chunk)
                    sent.append(len(chunk))
                    # Loopback is fast enough to send it all before an abort.
                    time.sleep(0.005)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):  # noqa: A002
            pass

    def handler(*args, **kwargs):
        return WholeFile(*args, directory=str(tmp_path), **kwargs)

    whole = _serve(handler)
    try:
        with pytest.raises(NetworkError, match="ignores HTTP range requests"):
            fetch_range(f"http://127.0.0.1:{whole.server_address[1]}/big.bin", 0, 127)
    finally:
        whole.shutdown()
        whole.server_close()
    # The cancel stops the transfer long before the end of the file.
    assert sum(sent) < 2 << 20


@pytest.mark.parametrize(
    ("received", "total", "cancelled"),
    [(0, 10, False), (10, 10, False), (4, -1, False), (11, -1, True), (0, 11, True)],
)
def test_cancel_past(received, total, cancelled):
    feedback = QgsFeedback()
    cancel_past(10, feedback)(received, total)
    assert feedback.isCanceled() is cancelled
