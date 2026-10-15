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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from portolan_registry_qgis.core.hrefs import asset_href, glob_root, is_glob
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.parquet_query import read_error
from portolan_registry_qgis.core.scope import intersects
from portolan_registry_qgis.core.stac import Document, StacError, read_fetched

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Asset, Node

    FetchJson = Callable[[str], object]
    ListGlob = Callable[[str], list[str]]
    Read = Callable[[str], Document | tuple[str, str] | None]

_UNSAFE = re.compile(r"[^A-Za-z0-9._@+=-]")
EXTERNAL_DIR = "_external"
WALK_WORKERS = 32


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
    # The items the plan leaves out, because they lie outside the scope.
    outside: int = 0
    # Each partition glob whose files could not be listed, and the reason.
    unlisted: list[tuple[str, str]] = field(default_factory=list)

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

    A URL under ``root`` keeps its relative path. Others go under
    ``_external/<host>/``. No result can climb out of the target folder.
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


def clipped_path(href: str, root: str) -> str:
    """Return where the GeoPackage of a clipped GeoParquet file goes in the download folder.

    It keeps the file's place in the catalog layout, with a ``.gpkg`` suffix.
    A glob writes one file named after the folder that holds its partitions.
    """
    if is_glob(href):
        folder = glob_root(href)
        href = f"{folder}/{posixpath.basename(folder) or 'partitions'}.parquet"
    path = local_path(href, root)
    folder, _sep, name = path.rpartition("/")
    stem = name
    for suffix in (".geoparquet", ".parquet"):
        if name.lower().endswith(suffix):
            stem = name[: -len(suffix)]
            break
    return f"{folder}/{stem}.gpkg" if folder else f"{stem}.gpkg"


def files_of(document: Document, root: str) -> list[PlannedFile]:
    """Return the document itself, its assets, and its PMTiles links.

    An archive that is both an asset and a link downloads once, as the
    asset, because the asset declares its size and checksum.
    """
    files = [PlannedFile(document.href, local_path(document.href, root))]
    files.extend(
        PlannedFile(asset.href, local_path(asset.href, root), asset.size, asset.checksum)
        for asset in document.assets
        if asset.href.startswith(("http://", "https://")) and asset.glob is None
    )
    shared = document.pmtiles_duplicates
    files.extend(
        PlannedFile(link.href, local_path(link.href, root))
        for link in document.pmtiles
        if link.href not in shared
    )
    return files


@dataclass(frozen=True)
class Walk:
    """How a walk reads the documents below its start.

    Without ``executor``, the walk reads one document at a time. With it, the
    walk reads ahead. QGIS callers pass an executor that runs on QThreads.
    """

    # Stops the walk after this many reads.
    max_documents: int = 5000
    # Polled before each read.
    cancelled: Callable[[], bool] | None = None
    # Runs the reads in parallel.
    executor: Executor | None = None
    # Lists the files of a bucket glob. Without it, partitioned assets are left out.
    list_glob: ListGlob | None = None


def partition_files(
    asset: Asset, root: str, list_glob: ListGlob | None
) -> tuple[list[PlannedFile], str | None]:
    """Return the files of a partitioned asset, or the reason they cannot be listed."""
    url = asset.read_url
    if url is None:
        return [], _("HTTPS cannot list the files of {glob}").format(glob=asset.glob)
    if list_glob is None:
        return [], _("Listing the files of {glob} needs DuckDB").format(glob=asset.glob)
    try:
        found = list_glob(url)
    except Exception as error:  # noqa: BLE001 - DuckDB raises its own error types
        return [], read_error(url, error)
    hrefs = [asset_href(listed, listed) for listed in found]
    return [PlannedFile(href, local_path(href, root)) for href in hrefs], None


def walk(
    fetch_json: FetchJson,
    start: str,
    failures: list[tuple[str, str]],
    how: Walk | None = None,
) -> Iterator[Document]:
    """Yield ``start`` and every catalog, collection, and item below it, breadth first.

    ``failures`` gets ``(url, reason)`` for each document that cannot be read.
    ``how`` None reads up to 5000 documents in the calling thread.
    """
    how = how or Walk()
    cancelled = how.cancelled

    def read(href: str) -> Document | tuple[str, str] | None:
        if cancelled is not None and cancelled():
            return None
        try:
            return read_fetched(fetch_json(href), href)
        except (OSError, ValueError, StacError) as error:
            return (href, str(error))

    if how.executor is None:
        yield from _walk_in_turn(read, start, failures, how.max_documents)
    else:
        yield from _walk_ahead(read, start, failures, how.max_documents, how.executor)


def _enqueue(children: tuple[Node, ...], seen: set[str], queue: deque[str]) -> None:
    """Queue each child that the walk has not queued yet."""
    for child in children:
        if child.href not in seen:
            seen.add(child.href)
            queue.append(child.href)


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
        _enqueue(result.children, seen, queue)


class _ReadAhead:
    """Start each read as soon as the parent document arrives.

    The reads run on ``executor``, up to ``max_documents`` of them.
    """

    def __init__(self, read: Read, executor: Executor, max_documents: int) -> None:
        self._read = read
        self._executor = executor
        self._max_documents = max_documents
        self._fetches: dict[str, Future[Document | tuple[str, str] | None]] = {}
        self._unsettled: set[Future[Document | tuple[str, str] | None]] = set()

    def fetch(self, href: str) -> None:
        """Start the read of ``href`` when it is new and the budget allows it."""
        if href not in self._fetches and len(self._fetches) < self._max_documents:
            future = self._executor.submit(self._read, href)
            self._fetches[href] = future
            self._unsettled.add(future)

    def _settle(self) -> None:
        """Start the reads of the children of each document that arrived."""
        for future in [f for f in self._unsettled if f.done()]:
            self._unsettled.discard(future)
            result = None if future.cancelled() else future.result()
            if isinstance(result, Document):
                for child in result.children:
                    self.fetch(child.href)

    def finished(self, href: str) -> Future[Document | tuple[str, str] | None] | None:
        """Wait for the read of ``href``.

        Returns:
            The finished read, or None when the budget ran out before it.
        """
        future = self._fetches.get(href)
        if future is None:
            return None
        while not future.done():
            wait(self._unsettled, return_when=FIRST_COMPLETED)
            self._settle()
        self._settle()
        return future

    def cancel(self) -> None:
        """Drop the reads that have not started."""
        for future in self._fetches.values():
            future.cancel()


def _walk_ahead(
    read: Read,
    start: str,
    failures: list[tuple[str, str]],
    max_documents: int,
    executor: Executor,
) -> Iterator[Document]:
    # Two orders run side by side. ``_ReadAhead`` starts a read as soon as
    # the parent document arrives. ``queue`` yields the documents in the
    # order of a plain breadth-first walk, so the plan does not depend on
    # timing.
    ahead = _ReadAhead(read, executor, max_documents)
    ahead.fetch(start)
    queue: deque[str] = deque([start])
    placed = {start}
    try:
        while queue:
            future = ahead.finished(queue.popleft())
            if future is None:
                # The read budget ran out before this document.
                continue
            result = future.result()
            if result is None:
                return
            if isinstance(result, tuple):
                failures.append(result)
                continue
            yield result
            _enqueue(result.children, placed, queue)
    finally:
        # A cancelled or abandoned walk drops the reads that have not started.
        ahead.cancel()


def split_collisions(files: list[PlannedFile]) -> tuple[list[PlannedFile], list[PlannedFile]]:
    """Return the files to download, and the later files that share a local path.

    The comparison ignores case, as on Windows and macOS.
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
    how: Walk | None = None,
    scope: Scope | None = None,
) -> Plan:
    """Plan the download of ``start`` and everything below it.

    ``root`` defaults to ``start``. ``scope`` drops the items whose bbox misses
    it. Catalogs, collections, and items with no bbox stay.
    """
    how = how or Walk()
    base = root or start
    failures: list[tuple[str, str]] = []
    unlisted: list[tuple[str, str]] = []
    files: dict[str, PlannedFile] = {}
    reads = outside = 0
    for document in walk(fetch_json, start, failures, how):
        reads += 1
        if scope is not None and document.kind == "item" and not intersects(document.bbox, scope):
            outside += 1
            continue
        for planned in files_of(document, base):
            files.setdefault(planned.url, planned)
        _add_partitions(document, base, how.list_glob, files, unlisted)
    truncated = reads + len(failures) >= how.max_documents
    return Plan(list(files.values()), failures, truncated, outside, unlisted)


def _add_partitions(
    document: Document,
    root: str,
    list_glob: ListGlob | None,
    files: dict[str, PlannedFile],
    unlisted: list[tuple[str, str]],
) -> None:
    """Add the files of each partitioned asset of ``document`` to ``files``.

    The walk reads the items, so a glob that HTTPS serves adds nothing when the document has some.
    """
    for asset in document.assets:
        if asset.glob is None or (asset.read_url is None and document.children):
            continue
        listed, reason = partition_files(asset, root, list_glob)
        if reason is not None:
            unlisted.append((asset.glob, reason))
        for planned in listed:
            files.setdefault(planned.url, planned)
