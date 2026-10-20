"""Fetch documents through QGIS's network stack.

Every request goes through ``QgsNetworkAccessManager``, so the user's proxy,
SSL, and authentication settings apply. A blocking request is safe off the
main thread, which is where ``run_task`` sends it.
"""

from __future__ import annotations

import json
import traceback
from concurrent.futures import Executor, Future
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsNetworkAccessManager,
    QgsNetworkReplyContent,
    QgsTask,
)
from qgis.PyQt.QtCore import QEventLoop, QRunnable, QThreadPool, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from portolan_registry_qgis.core.hrefs import is_http_url
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.stac import Fetched
from portolan_registry_qgis.qgis_io.log import log

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
    """Return the body at ``url``. ``accept`` is the ``Accept`` header.

    Raises:
        NetworkError: The request failed or the server answered with an error.
    """
    return _fetch(url, accept)[0]


# No request here uses a QObject that Python creates. QGIS calls the
# disconnectNotify of such an object with a Qt signal lock held, and the sip
# override then waits for the GIL. The main thread can hold the GIL and wait
# for the same lock, and then both threads stop. QgsNetworkAccessManager
# makes its blocking request in C++, and a raw reply is a C++ object.


def _fetch(url: str, accept: bytes = _ACCEPT) -> tuple[bytes, str]:
    """Return the body at ``url`` and the URL it came from after any redirect."""
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Accept", accept)
    reply = QgsNetworkAccessManager.blockingGet(request, forceRefresh=False)
    _raise_for(reply, url)
    final = reply.request().url().toString()
    return bytes(reply.content()), final or url


def _raise_for(reply: QgsNetworkReplyContent | QNetworkReply, url: str) -> None:
    """Raise a ``NetworkError`` when ``reply`` ended with an error."""
    if reply.error() != QNetworkReply.NetworkError.NoError:
        # Translators: An error about the file at {url}.
        raise NetworkError(_("{url}: {error}").format(url=url, error=reply.errorString()))


def http_status(url: str) -> tuple[int | None, str]:
    """Ask for the first byte of ``url`` and return the HTTP status and any error.

    The status is None when no HTTP answer came. QGIS uses its own proxy
    settings, so the answer can differ from what GDAL gets.
    """
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Range", b"bytes=0-0")
    request.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
    reply = QgsNetworkAccessManager.blockingGet(request, forceRefresh=True)
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    error = "" if reply.error() == QNetworkReply.NetworkError.NoError else reply.errorString()
    return (int(status) if status else None), error


def fetch_range(url: str, offset: int, length: int) -> bytes:
    """Return ``length`` bytes of ``url`` from ``offset``.

    The read cancels a 200 reply or a body longer than ``length``, and raises
    NetworkError. It runs a local event loop, so call it off the main thread.
    """
    return _range(url, offset, length)[0]


def _range(url: str, offset: int, length: int) -> tuple[bytes, bytes]:
    """Return the range and the reply's Cache-Control header."""
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Range", f"bytes={offset}-{offset + length - 1}".encode())
    # Qt's disk cache keys on the URL alone. A cached range would answer a
    # request for a different range, so ranges bypass the cache both ways.
    request.setAttribute(
        QNetworkRequest.Attribute.CacheLoadControlAttribute,
        QNetworkRequest.CacheLoadControl.AlwaysNetwork,
    )
    request.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
    # Qt keeps the Range header when it follows a redirect.
    request.setAttribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute,
        QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
    )
    reply = QgsNetworkAccessManager.instance().get(request)
    cut: list[bool] = []

    def cancel() -> None:
        cut.append(True)
        reply.abort()

    def check_status() -> None:
        if reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) == 200:
            cancel()

    check_size = cancel_past(length, cancel)
    loop = QEventLoop()
    reply.metaDataChanged.connect(check_status)
    reply.downloadProgress.connect(check_size)
    reply.finished.connect(loop.quit)
    try:
        if not reply.isFinished():
            loop.exec()
        if cut or reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) == 200:
            raise NetworkError(_("{url}: the server ignores HTTP range requests").format(url=url))
        _raise_for(reply, url)
        return bytes(reply.readAll()), bytes(reply.rawHeader(b"Cache-Control"))
    finally:
        # The slots go first, so a signal from the reply's destructor reaches no slot.
        reply.metaDataChanged.disconnect(check_status)
        reply.downloadProgress.disconnect(check_size)
        reply.deleteLater()


def cancel_past(length: int, cancel: Callable[[], None]) -> Callable[[int, int], None]:
    """Return a ``downloadProgress`` slot that calls ``cancel`` for a body longer than ``length``.

    A 206 reply holds ``length`` bytes or fewer. More bytes, received or
    announced, mean that the server sends the whole file.
    """

    def check(received: int, total: int) -> None:
        if received > length or total > length:
            cancel()

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
    if is_http_url(location):
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
    return fetch_document(url).document


def fetch_document(url: str) -> Fetched:
    """Return the parsed JSON at ``url`` and the URL it came from, which relative hrefs need.

    Raises NetworkError or ValueError as ``fetch_json`` does.
    """
    body, final = _fetch(url)
    try:
        return Fetched(json.loads(body), final)
    except ValueError as error:
        raise ValueError(
            _("{url} did not return JSON: {error}").format(url=url, error=error)
        ) from error


def report_failure(text: str) -> None:
    """Write a failure that no message reports to the QGIS message log."""
    log(text, Qgis.MessageLevel.Critical)


def run_task(
    description: str,
    function: Callable[[QgsTask], object],
    on_done: Callable[[object, BaseException | None], None],
    *,
    hidden: bool = False,
) -> QgsTask:
    """Run ``function`` in a queued background task, and return the task.

    ``on_done`` gets ``(result, None)`` or ``(None, error)`` on the main thread.
    ``hidden`` keeps small reads, such as icons, out of the task manager.
    """

    def finished(exception: BaseException | None, result: object = None) -> None:
        _RUNNING.discard(task)
        if exception is None and result is None and task.isCanceled():
            # Translators: Why a task stopped. The user cancelled it.
            exception = NetworkError(_("Cancelled"))
        try:
            on_done(None if exception else result, exception)
        except Exception:  # noqa: BLE001 - QGIS drops an error that leaves this function
            # QGIS swallows an exception in a task's finished handler. Then
            # an action that fails looks like an action that did nothing.
            report_failure(
                _("{task} failed after the task ended:\n{trace}").format(
                    task=description, trace=traceback.format_exc()
                )
            )

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
    """Run functions on QThreads, where a blocking request has Qt timers.

    The executor has its own ``QThreadPool``, so a task that waits for it
    cannot take every thread of the global pool.
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

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:  # noqa: FBT001, FBT002 - the signature of Executor.shutdown
        """Wait for the running functions when ``wait`` is true."""
        if cancel_futures:
            # A cancelled future's job returns at once when its turn comes.
            for future in list(self._futures):
                future.cancel()
        if wait:
            self._pool.waitForDone()
