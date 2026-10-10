"""Plan the download of a catalog, collection, or item to disk.

The plan keeps the catalog's own layout. Portolan catalogs link with relative
hrefs, so saving each STAC document next to its assets at the same relative
path gives a local copy that STAC tools can open.
"""

from __future__ import annotations

import posixpath
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from portolan_registry_qgis.core.stac import Document, StacError, read_document

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    FetchJson = Callable[[str], object]

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
    workers: int = WALK_WORKERS,
) -> Iterator[Document]:
    """Read ``start`` and every catalog, collection, and item below it.

    The walk reads one breadth-first level at a time. A pool of ``workers``
    threads reads the documents of a level in parallel, so a level of 500
    items costs about 500 / ``workers`` round trips, not 500.

    Args:
        fetch_json: Fetches a URL and returns the parsed JSON. It raises on
            failure. The walk calls it from several threads at once.
        start: The document to begin with.
        failures: Receives ``(url, reason)`` for each document that could
            not be read. The walk continues past it.
        max_documents: Stops the walk after this many reads.
        cancelled: Polled before each read.
        workers: The number of documents read at the same time.

    Yields:
        Each document read, breadth first, in link order.
    """

    def read(href: str) -> Document | tuple[str, str] | None:
        if cancelled is not None and cancelled():
            return None
        try:
            return read_document(fetch_json(href), href)
        except (OSError, ValueError, StacError) as error:
            return (href, str(error))

    level = [start]
    seen = {start}
    reads = 0
    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="stac-walk")
    try:
        while level and reads < max_documents:
            level = level[: max_documents - reads]
            reads += len(level)
            following: list[str] = []
            # map() returns the results in link order, so the plan is stable.
            for result in pool.map(read, level):
                if result is None:
                    return
                if isinstance(result, tuple):
                    failures.append(result)
                    continue
                yield result
                for child in result.children:
                    if child.href not in seen:
                        seen.add(child.href)
                        following.append(child.href)
            level = following
    finally:
        # A cancelled or abandoned walk drops the reads it has not started.
        pool.shutdown(wait=True, cancel_futures=True)


def plan(
    fetch_json: FetchJson,
    start: str,
    root: str | None = None,
    max_documents: int = 5000,
    cancelled: Callable[[], bool] | None = None,
    workers: int = WALK_WORKERS,
) -> Plan:
    """Plan the download of ``start`` and everything below it.

    Args:
        fetch_json: Fetches a URL and returns the parsed JSON.
        start: The catalog, collection, or item to download.
        root: The URL that local paths are relative to. Defaults to
            ``start``, so the chosen document lands at the top of the folder.
        max_documents: Stops the walk after this many reads.
        cancelled: Polled before each read.
        workers: The number of documents read at the same time.
    """
    base = root or start
    failures: list[tuple[str, str]] = []
    files: dict[str, PlannedFile] = {}
    reads = 0
    for document in walk(fetch_json, start, failures, max_documents, cancelled, workers):
        reads += 1
        for planned in files_of(document, base):
            files.setdefault(planned.url, planned)
    truncated = reads + len(failures) >= max_documents
    return Plan(list(files.values()), failures, truncated=truncated)
