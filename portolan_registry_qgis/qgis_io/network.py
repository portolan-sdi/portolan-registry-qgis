"""Fetch documents through QGIS's network stack.

Every request goes through ``QgsBlockingNetworkRequest``, so the user's proxy,
SSL, and authentication settings apply. A blocking request is safe off the
main thread, which is where ``run_task`` sends it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.core import QgsApplication, QgsBlockingNetworkRequest, QgsFeedback, QgsTask
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

if TYPE_CHECKING:
    from collections.abc import Callable

_ACCEPT = b"application/geo+json, application/json"
# QgsTask.fromFunction tasks are owned by the task manager, but the Python
# wrapper is not. Without a reference the wrapper is collected mid-run and its
# on_finished callback never fires.
_RUNNING: set[Any] = set()


class NetworkError(OSError):
    """A request failed or returned an HTTP error."""


def fetch_bytes(url: str, accept: bytes = _ACCEPT) -> bytes:
    """Return the body at ``url``.

    Args:
        url: The address to read.
        accept: The ``Accept`` header. The default asks for JSON documents.

    Raises:
        NetworkError: The request failed or the server answered with an error.
    """
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Accept", accept)
    blocking = QgsBlockingNetworkRequest()
    code = blocking.get(request, forceRefresh=False)
    if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise NetworkError(f"{url}: {blocking.errorMessage()}")
    return bytes(blocking.reply().content())


def fetch_range(url: str, offset: int, length: int) -> bytes:
    """Return ``length`` bytes of ``url`` from ``offset``.

    A server that ignores the Range header answers 200 with the whole body.
    QGIS 3.38 and later abort that reply. QGIS 3.34 and 3.36 read it to the
    end, so the function cancels it when the body grows past ``length``.
    Without the cancel, every tile read downloads the whole archive.

    Raises:
        NetworkError: The request failed, or the server ignores ranges.
    """
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Range", f"bytes={offset}-{offset + length - 1}".encode())
    # Qt's disk cache keys on the URL alone. A cached range would answer a
    # request for a different range, so ranges bypass the cache both ways.
    request.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
    feedback = QgsFeedback()
    blocking = QgsBlockingNetworkRequest()
    check = cancel_past(length, feedback)
    # Direct, because the calling thread runs no event loop. The slot
    # disconnects before the function returns, because the request's
    # destructor aborts its reply and emits progress on whatever thread
    # collects it.
    blocking.downloadProgress.connect(check, Qt.ConnectionType.DirectConnection)
    try:
        code = blocking.get(request, forceRefresh=True, feedback=feedback)
    finally:
        blocking.downloadProgress.disconnect(check)
    reply = blocking.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if feedback.isCanceled() or status == 200:
        raise NetworkError(f"{url}: the server ignores HTTP range requests")
    if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise NetworkError(f"{url}: {blocking.errorMessage()}")
    return bytes(reply.content())


def cancel_past(length: int, feedback: QgsFeedback) -> Callable[[int, int], None]:
    """Return a ``downloadProgress`` slot that cancels a body longer than ``length``.

    A 206 reply holds ``length`` bytes or fewer. More bytes, received or
    announced, mean that the server sends the whole file.
    """

    def check(received: int, total: int) -> None:
        if received > length or total > length:
            feedback.cancel()

    return check


def range_fetcher(location: str) -> Callable[[int, int], bytes]:
    """Return a ``fetch_range(offset, length)`` for a URL or a local path."""
    if location.startswith(("http://", "https://")):
        return lambda offset, length: fetch_range(location, offset, length)
    path = Path(QUrl(location).toLocalFile() if location.startswith("file:") else location)

    def read(offset: int, length: int) -> bytes:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    return read


def fetch_json(url: str) -> object:
    """Return the parsed JSON at ``url``.

    Raises:
        NetworkError: The request failed.
        ValueError: The body is not JSON.
    """
    body = fetch_bytes(url)
    try:
        return json.loads(body)
    except ValueError as error:
        raise ValueError(f"{url} did not return JSON: {error}") from error


def run_task(
    description: str,
    function: Callable[[QgsTask], object],
    on_done: Callable[[object, BaseException | None], None],
    hidden: bool = False,
) -> QgsTask:
    """Run ``function`` in a background task and report back on the main thread.

    Args:
        description: Shown in the QGIS task manager.
        function: Called with the task, off the main thread.
        on_done: Called with ``(result, None)`` on success or
            ``(None, error)`` on failure or cancellation.
        hidden: Keep the task out of the task manager. Use it for small
            reads the user did not ask for, such as icons.

    Returns:
        The task, already queued.
    """

    def finished(exception: BaseException | None, result: object = None) -> None:
        _RUNNING.discard(task)
        if exception is None and result is None and task.isCanceled():
            exception = NetworkError("Cancelled")
        on_done(None if exception else result, exception)

    flags = QgsTask.Flag.CanCancel
    if hidden:
        flags = flags | QgsTask.Flag.Hidden | QgsTask.Flag.Silent
    task = QgsTask.fromFunction(description, function, on_finished=finished, flags=flags)
    _RUNNING.add(task)
    QgsApplication.taskManager().addTask(task)
    return task
