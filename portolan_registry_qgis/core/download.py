"""Plan the download of a catalog, collection, or item to disk.

The plan keeps the catalog's own layout. Portolan catalogs link with relative
hrefs, so saving each STAC document next to its assets at the same relative
path gives a local copy that STAC tools can open.
"""

from __future__ import annotations

import posixpath
import re
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Executor, Future, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from portolan_registry_qgis.core.stac import Document, StacError, read_document

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    FetchJson = Callable[[str], object]
    Read = Callable[[str], Document | tuple[str, str] | None]

_UNSAFE = re.compile(r"[^A-Za-z0-9._@+=-]")
EXTERNAL_DIR = "_external"
WALK_WORKERS = 8


@dataclass(frozen=True)
class PlannedFile:
    """One file to download, and where it goes under the target folder."""

    url: str
    path: str
    size: int | None = None
    checksum: str | None = None


@dataclass
class Plan:
    """The files to download, and the documents the walk could not read."""

    files: list[PlannedFile]
    failures: list[tuple[str, str]]
    truncated: bool = False

    @property
    def known_bytes(self) -> int:
        """The sum of the sizes the catalog declares."""
        return sum(file.size or 0 for file in self.files)

    @property
    def unknown_sizes(self) -> int:
        """How many files have no declared size."""
        return sum(1 for file in self.files if file.size is None)


def _safe_segments(path: str) -> list[str]:
    segments = []
    for segment in unquote(path).split("/"):
        if segment in {"", ".", ".."}:
            continue
        segments.append(_UNSAFE.sub("_", segment))
    return segments


def local_path(url: str, root: str) -> str:
    """Return the POSIX path, relative to the target folder, for ``url``.

    A URL under the directory of ``root`` keeps its relative path. Anything
    else goes under ``_external/<host>/``. No result can climb out of the
    target folder, because ``..`` segments are dropped and each segment is
    reduced to a safe character set.
    """
    target, base = urlsplit(url), urlsplit(root)
    base_dir = posixpath.dirname(base.path).rstrip("/") + "/"
    if (target.scheme, target.netloc) == (base.scheme, base.netloc) and target.path.startswith(
        base_dir
    ):
        segments = _safe_segments(target.path[len(base_dir) :])
    else:
        segments = [EXTERNAL_DIR, *_safe_segments(target.netloc), *_safe_segments(target.path)]
    if not segments or segments == [EXTERNAL_DIR]:
        segments.append("index")
    return "/".join(segments)


def files_of(document: Document, root: str) -> list[PlannedFile]:
    """Return the document itself, its assets, and its PMTiles links."""
    files = [PlannedFile(document.href, local_path(document.href, root))]
    files.extend(
        PlannedFile(asset.href, local_path(asset.href, root), asset.size, asset.checksum)
        for asset in document.assets
        if asset.href.startswith(("http://", "https://"))
    )
    asset_urls = {asset.href for asset in document.assets}
    files.extend(
        PlannedFile(link.href, local_path(link.href, root))
        for link in document.pmtiles
        if link.href not in asset_urls
    )
    return files


def walk(
    fetch_json: FetchJson,
    start: str,
    failures: list[tuple[str, str]],
    max_documents: int = 5000,
    cancelled: Callable[[], bool] | None = None,
    executor: Executor | None = None,
) -> Iterator[Document]:
    """Read ``start`` and every catalog, collection, and item below it.

    Without ``executor``, the walk reads one document at a time in the
    calling thread. With ``executor``, the walk reads ahead. When a document
    arrives, the walk queues its children at once, so one slow document does
    not stop the reads below its siblings. The caller owns the executor. QGIS
    network requests need QThreads, so a QGIS caller passes an executor that
    runs on QThreads.

    Args:
        fetch_json: Fetches a URL and returns the parsed JSON. It raises on
            failure. With ``executor``, the walk calls it from the
            executor's threads.
        start: The document to begin with.
        failures: Receives ``(url, reason)`` for each document that could
            not be read. The walk continues past it.
        max_documents: Stops the walk after this many reads.
        cancelled: Polled before each read.
        executor: Runs the reads in parallel.

    Yields:
        Each document read, breadth first, in link order. A walk that
        reaches ``max_documents`` with ``executor`` can read a different
        last few documents from one run to the next.
    """

    def read(href: str) -> Document | tuple[str, str] | None:
        if cancelled is not None and cancelled():
            return None
        try:
            return read_document(fetch_json(href), href)
        except (OSError, ValueError, StacError) as error:
            return (href, str(error))

    if executor is None:
        yield from _walk_in_turn(read, start, failures, max_documents)
    else:
        yield from _walk_ahead(read, start, failures, max_documents, executor)


def _walk_in_turn(
    read: Read, start: str, failures: list[tuple[str, str]], max_documents: int
) -> Iterator[Document]:
    queue: deque[str] = deque([start])
    seen = {start}
    reads = 0
    while queue and reads < max_documents:
        href = queue.popleft()
        reads += 1
        result = read(href)
        if result is None:
            return
        if isinstance(result, tuple):
            failures.append(result)
            continue
        yield result
        for child in result.children:
            if child.href not in seen:
                seen.add(child.href)
                queue.append(child.href)


def _walk_ahead(
    read: Read,
    start: str,
    failures: list[tuple[str, str]],
    max_documents: int,
    executor: Executor,
) -> Iterator[Document]:
    # Two orders run side by side. ``fetches`` starts a read as soon as the
    # parent document arrives. ``queue`` yields the documents in the order
    # of a plain breadth-first walk, so the plan does not depend on timing.
    fetches: dict[str, Future[Document | tuple[str, str] | None]] = {}
    unsettled: set[Future[Document | tuple[str, str] | None]] = set()

    def fetch(href: str) -> None:
        if href not in fetches and len(fetches) < max_documents:
            future = executor.submit(read, href)
            fetches[href] = future
            unsettled.add(future)

    def settle() -> None:
        for future in [f for f in unsettled if f.done()]:
            unsettled.discard(future)
            result = None if future.cancelled() else future.result()
            if isinstance(result, Document):
                for child in result.children:
                    fetch(child.href)

    fetch(start)
    queue: deque[str] = deque([start])
    placed = {start}
    try:
        while queue:
            future = fetches.get(queue.popleft())
            if future is None:
                # The read budget ran out before this document.
                continue
            while not future.done():
                wait(unsettled, return_when=FIRST_COMPLETED)
                settle()
            settle()
            result = future.result()
            if result is None:
                return
            if isinstance(result, tuple):
                failures.append(result)
                continue
            yield result
            for child in result.children:
                if child.href not in placed:
                    placed.add(child.href)
                    queue.append(child.href)
    finally:
        # A cancelled or abandoned walk drops the reads that have not started.
        for future in fetches.values():
            future.cancel()


def split_collisions(files: list[PlannedFile]) -> tuple[list[PlannedFile], list[PlannedFile]]:
    """Separate the files that would save over an earlier file.

    Two URLs can map to one local path, because ``local_path`` replaces
    unsafe characters. Windows and macOS also treat ``Data.tif`` and
    ``data.tif`` as one file, so the comparison ignores case.

    Returns:
        The files to download, and the later files that share a path.
    """
    claimed: set[str] = set()
    keep: list[PlannedFile] = []
    clashes: list[PlannedFile] = []
    for planned in files:
        key = planned.path.casefold()
        if key in claimed:
            clashes.append(planned)
        else:
            claimed.add(key)
            keep.append(planned)
    return keep, clashes


def plan(
    fetch_json: FetchJson,
    start: str,
    root: str | None = None,
    max_documents: int = 5000,
    cancelled: Callable[[], bool] | None = None,
    executor: Executor | None = None,
) -> Plan:
    """Plan the download of ``start`` and everything below it.

    Args:
        fetch_json: Fetches a URL and returns the parsed JSON.
        start: The catalog, collection, or item to download.
        root: The URL that local paths are relative to. Defaults to
            ``start``, so the chosen document lands at the top of the folder.
        max_documents: Stops the walk after this many reads.
        cancelled: Polled before each read.
        executor: Runs the reads in parallel. See ``walk``.
    """
    base = root or start
    failures: list[tuple[str, str]] = []
    files: dict[str, PlannedFile] = {}
    reads = 0
    for document in walk(fetch_json, start, failures, max_documents, cancelled, executor):
        reads += 1
        for planned in files_of(document, base):
            files.setdefault(planned.url, planned)
    truncated = reads + len(failures) >= max_documents
    return Plan(list(files.values()), failures, truncated=truncated)
