"""An HTTP server on loopback for the tests of both suites."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from socketserver import BaseRequestHandler


@contextmanager
def serve(handler: Callable[..., BaseRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    """Serve ``handler`` on a free loopback port until the block ends.

    The server stops, closes its socket, and its thread ends before the
    block returns. A failed test therefore leaves no server behind.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def base_url(server: ThreadingHTTPServer) -> str:
    """Return the ``http://127.0.0.1:<port>`` address of ``server``."""
    return f"http://127.0.0.1:{server.server_address[1]}"
