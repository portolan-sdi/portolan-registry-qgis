"""Fetch documents through QGIS's network stack.

Every request goes through ``QgsBlockingNetworkRequest``, so the user's proxy,
SSL, and authentication settings apply. A blocking request is safe off the
main thread, which is where ``run_task`` sends it.
"""

from __future__ import annotations

import json
from concurrent.futures import Executor, Future
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.core import QgsApplication, QgsBlockingNetworkRequest, QgsFeedback, QgsTask
from qgis.PyQt.QtCore import QRunnable, Qt, QThreadPool, QUrl
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
    return _range(url, offset, length)[0]


def _range(url: str, offset: int, length: int) -> tuple[bytes, bytes]:
    """Return the range and the reply's Cache-Control header."""
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Range", f"bytes={offset}-{offset + length - 1}".encode())
    # Qt's disk cache keys on the URL alone. A cached range would answer a
    # request for a different range, so ranges bypass the cache both ways.
    request.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
    # QGIS 3.34 and 3.36 follow a redirect themselves and drop the Range
    # header, so the target answers 200 with the whole file. Qt keeps the
    # header when it follows the redirect, as QGIS 3.38 and later make it do.
    request.setAttribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute,
        QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
    )
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
    return bytes(reply.content()), bytes(reply.rawHeader(b"Cache-Control"))


def cancel_past(length: int, feedback: QgsFeedback) -> Callable[[int, int], None]:
    """Return a ``downloadProgress`` slot that cancels a body longer than ``length``.

    A 206 reply holds ``length`` bytes or fewer. More bytes, received or
    announced, mean that the server sends the whole file.
    """

    def check(received: int, total: int) -> None:
        if received > length or total > length:
            feedback.cancel()

    return check


class RemoteRange:
    """``fetch_range`` bound to one URL.

    ``no_store`` turns true once the server answers with
    ``Cache-Control: no-store``. The tile server then keeps the archive's
    tiles out of the QGIS disk cache.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.no_store = False

    def __call__(self, offset: int, length: int) -> bytes:
        """Return ``length`` bytes from ``offset``."""
        body, cache_control = _range(self.url, offset, length)
        if b"no-store" in cache_control.lower():
            self.no_store = True
        return body


def range_fetcher(location: str) -> Callable[[int, int], bytes]:
    """Return a ``fetch_range(offset, length)`` for a URL or a local path."""
    if location.startswith(("http://", "https://")):
        return RemoteRange(location)
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


class _Job(QRunnable):
    def __init__(self, future: Future[Any], function: Callable[[], Any]):
        super().__init__()
        self._future = future
        self._function = function

    def run(self) -> None:
        # A job whose future was cancelled before it started does nothing.
        if not self._future.set_running_or_notify_cancel():
            return
        try:
            result = self._function()
        except BaseException as error:  # noqa: BLE001 - the future carries it
            self._future.set_exception(error)
        else:
            self._future.set_result(result)


class QtExecutor(Executor):
    """Run functions on QThreads, so QGIS network requests can use them.

    ``QgsBlockingNetworkRequest`` needs Qt timers, and Qt timers work only on
    a QThread. A ``ThreadPoolExecutor`` thread is a plain Python thread. The
    executor has its own ``QThreadPool``, so a task that waits for it cannot
    take every thread of the global pool.
    """

    def __init__(self, workers: int):
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max(1, workers))
        self._futures: set[Future[Any]] = set()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        """Queue ``fn(*args, **kwargs)`` and return its future."""
        future: Future[Any] = Future()
        self._futures.add(future)
        future.add_done_callback(self._futures.discard)
        self._pool.start(_Job(future, lambda: fn(*args, **kwargs)))
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Wait for the running functions when ``wait`` is true."""
        if cancel_futures:
            # A cancelled future's job returns at once when its turn comes.
            for future in list(self._futures):
                future.cancel()
        if wait:
            self._pool.waitForDone()
