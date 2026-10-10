"""Serve PMTiles archives to QGIS as XYZ vector tiles.

QGIS has no PMTiles vector tile provider. This module reads each archive with
the plugin's own PMTiles v3 reader (``core.pmtiles``) and puts a loopback HTTP
server in front of it, so a native ``QgsVectorTileLayer`` with ``type=xyz``
can render the tiles with a converted MapLibre style.

The reader fetches byte ranges through QGIS's network stack, not through
GDAL, so it needs no particular GDAL build and honors the QGIS proxy and
authentication settings.

The server binds 127.0.0.1 on a free port and answers only for archives the
plugin registered, each under a random token.

The server speaks HTTP/1.1, so QGIS keeps its tile connections open. Each
connection has one handler thread, and QGIS gives each thread its own network
manager. A kept connection therefore keeps its connection to the remote host,
and a tile read does not pay for a new TLS handshake.

Tile responses allow caching for a day, so QGIS keeps them in its disk cache
for the session. The tokens and the port change in each session, so a later
session does not reuse them. A server that answers with
``Cache-Control: no-store`` keeps that rule for its tiles. A changed archive
gets a new token, so new layers of it do not draw tiles cached from the old
archive.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from portolan_registry_qgis.core.pmtiles import PmtilesError, Reader

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["Archive", "PmtilesError", "TileServer", "open_archive", "read_tile"]


@dataclass(frozen=True)
class Archive:
    """A registered PMTiles archive."""

    url: str
    min_zoom: int
    max_zoom: int
    bounds: tuple[float, float, float, float] | None
    vector_layers: tuple[str, ...]
    reader: Reader = field(compare=False, repr=False)
    # The server answered Cache-Control: no-store, so no tile may reach disk.
    no_store: bool = False


def open_archive(url: str, fetch_range: Callable[[int, int], bytes] | None = None) -> Archive:
    """Read an archive's header and metadata.

    Args:
        url: The archive's http(s) URL, or a local path.
        fetch_range: Reads ``length`` bytes at ``offset``. Defaults to QGIS's
            network stack for a URL and to the file system for a path.

    Raises:
        PmtilesError: The archive cannot be read, or it holds raster tiles.
        OSError: The request failed.
    """
    if fetch_range is None:
        from portolan_registry_qgis.qgis_io.network import range_fetcher

        fetch_range = range_fetcher(url)
    reader = Reader(fetch_range)
    try:
        header = reader.header()
    except PmtilesError as error:
        raise PmtilesError(f"{url}: {error}") from error
    if header.tile_type != "mvt":
        raise PmtilesError(f"{url} holds {header.tile_type} tiles, not vector tiles")
    metadata = reader.metadata()
    layers = metadata.get("vector_layers")
    names = (
        tuple(str(layer["id"]) for layer in layers if isinstance(layer, dict) and "id" in layer)
        if isinstance(layers, list)
        else ()
    )
    return Archive(
        url=url,
        min_zoom=header.min_zoom,
        max_zoom=header.max_zoom,
        bounds=header.bounds,
        vector_layers=names,
        reader=reader,
        # network.RemoteRange records it. Other fetchers have no such header.
        no_store=bool(getattr(fetch_range, "no_store", False)),
    )


def read_tile(archive: Archive, z: int, x: int, y: int) -> bytes | None:
    """Return one tile, decompressed, or None when the archive has no such tile."""
    return archive.reader.tile(z, x, y)


# The tokens last one QGIS session, so a day outlives every tile URL.
CACHE_CONTROL = "max-age=86400"


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"
    # The handler writes the headers and the body in two writes. On a kept
    # connection, Nagle's algorithm holds the second write until the client
    # acknowledges the first, which delayed acknowledgment stalls for 40 ms.
    disable_nagle_algorithm = True
    # Close a connection that QGIS leaves idle, so its thread ends.
    timeout = 60

    def do_GET(self) -> None:  # noqa: N802 - name set by BaseHTTPRequestHandler
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        archive = self.server.archives.get(parts[0]) if parts else None
        if archive is None or len(parts) != 4 or not parts[3].endswith(".pbf"):
            self.send_error(404)
            return
        try:
            z, x, y = int(parts[1]), int(parts[2]), int(parts[3][: -len(".pbf")])
        except ValueError:
            self.send_error(404)
            return
        try:
            tile = read_tile(archive, z, x, y)
        except OSError:
            self.send_error(502)
            return
        if not tile:
            # An absent tile is empty ocean, not an error. 204 keeps QGIS from
            # logging a network failure for each one.
            self.send_response(204)
            self.send_header("Cache-Control", _cache_control(archive))
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.mapbox-vector-tile")
        self.send_header("Content-Length", str(len(tile)))
        self.send_header("Cache-Control", _cache_control(archive))
        self.end_headers()
        self.wfile.write(tile)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        """Silence per-request logging; QGIS has its own network log."""


def _cache_control(archive: Archive) -> str:
    return "no-store" if archive.no_store else CACHE_CONTROL


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.archives: dict[str, Archive] = {}


class TileServer:
    """A loopback server for every PMTiles archive the plugin has opened."""

    def __init__(self) -> None:
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        # Each archive URL maps to its token and the archive behind it.
        self._known: dict[str, tuple[str, Archive]] = {}
        self._lock = threading.Lock()

    def _ensure_started(self) -> _Server:
        if self._server is None:
            self._server = _Server()
            self._thread = threading.Thread(
                target=self._server.serve_forever, name="portolan-pmtiles", daemon=True
            )
            self._thread.start()
        return self._server

    def register(self, archive: Archive) -> str:
        """Serve ``archive`` and return its XYZ URL template.

        An archive with the same URL and header as a registered one keeps the
        registered reader and token. Its directory cache and the tiles QGIS
        cached stay valid. A changed header means that the archive changed, so
        it gets a new token. Older layers of that URL read the new archive.
        """
        with self._lock:
            server = self._ensure_started()
            known = self._known.get(archive.url)
            if (
                known is not None
                and known[1].reader.header() == archive.reader.header()
                and known[1].no_store == archive.no_store
            ):
                token, archive = known
            else:
                token = secrets.token_urlsafe(16)
                self._known[archive.url] = (token, archive)
                for old, served in server.archives.items():
                    if served.url == archive.url:
                        server.archives[old] = archive
            server.archives[token] = archive
            port = server.server_address[1]
        return f"http://127.0.0.1:{port}/{token}/{{z}}/{{x}}/{{y}}.pbf"

    def serves(self, source: str) -> bool:
        """Return whether a layer source points at an archive this server serves."""
        with self._lock:
            if self._server is None:
                return False
            prefix = f"127.0.0.1:{self._server.server_address[1]}/"
            # QGIS 4 percent-encodes the url parameter in a layer source.
            decoded = unquote(source)
            return any(f"{prefix}{token}/" in decoded for token in self._server.archives)

    def stop(self) -> None:
        """Shut the server down. Layers that use it stop drawing."""
        with self._lock:
            if self._server is not None:
                # A kept connection outlives the listener. With no archives,
                # it answers 404.
                self._server.archives.clear()
                self._server.shutdown()
                self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server = self._thread = None
            self._known.clear()
