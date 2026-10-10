"""Download a planned set of files and verify their checksums.

``QgsFileDownloader`` streams each file to disk on the main thread's event
loop, so a 1 GB GeoParquet file never sits in memory and QGIS stays
responsive. The job runs ``CONCURRENT_FILES`` files at the same time. A file
that declares ``file:checksum`` is hashed in a background task, and that slot
waits for the hash while the other slots keep downloading. A file already on
disk with a matching checksum is skipped, so a second run resumes an
interrupted download.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from qgis.core import QgsFileDownloader, QgsTask
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal

from portolan_registry_qgis.core.multihash import ChecksumError, file_matches
from portolan_registry_qgis.qgis_io.network import run_task

if TYPE_CHECKING:
    from pathlib import Path

    from portolan_registry_qgis.core.download import PlannedFile

CONCURRENT_FILES = 3


@dataclass
class DownloadReport:
    """What a download run did."""

    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    verified: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


def target_path(folder: Path, planned: PlannedFile) -> Path:
    """Return where ``planned`` goes, refusing any path outside ``folder``."""
    path = (folder / planned.path).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError(f"{planned.path} would land outside {folder}")
    return path


class DownloadJob(QObject):
    """Download files into a folder, a few at the same time.

    Signals:
        progress: ``(files_done, files_total, message)``.
        finished: The ``DownloadReport``.
    """

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)

    def __init__(
        self,
        folder: Path,
        files: list[PlannedFile],
        parent: QObject | None = None,
        concurrency: int = CONCURRENT_FILES,
    ):
        super().__init__(parent)
        self._folder = folder
        self._total = len(files)
        self._pending = deque(files)
        self._concurrency = max(1, concurrency)
        self._busy = 0
        self._settled = 0
        self._report = DownloadReport()
        self._downloaders: set[QgsFileDownloader] = set()
        self._tasks: set[QgsTask] = set()
        self._cancelled = False
        self._done = False
        self._pumping = False

    def start(self) -> None:
        """Begin with the first files."""
        self._pump()

    def cancel(self) -> None:
        """Abort every request and hash in flight, then report."""
        self._cancelled = True
        self._report.cancelled = True
        for downloader in list(self._downloaders):
            downloader.cancelDownload()
        for task in list(self._tasks):
            task.cancel()
        self._pump()

    def _finish(self) -> None:
        if not self._done:
            self._done = True
            self.finished.emit(self._report)

    def _pump(self) -> None:
        """Start files until every slot is busy, or report when all are done."""
        # A downloader can fail inside startDownload, which calls back here.
        # The loop below picks up that freed slot, so a nested call returns.
        if self._pumping or self._done:
            return
        self._pumping = True
        try:
            while not self._cancelled and self._pending and self._busy < self._concurrency:
                self._begin(self._pending.popleft())
        finally:
            self._pumping = False
        if self._busy == 0 and (self._cancelled or not self._pending):
            self._finish()

    def _begin(self, planned: PlannedFile) -> None:
        self.progress.emit(self._settled, self._total, planned.path)
        try:
            path = target_path(self._folder, planned)
        except ValueError as error:
            self._report.failed.append((planned.path, str(error)))
            self._settled += 1
            return
        if path.exists() and planned.checksum:
            self._busy += 1
            self._verify(planned, path, existing=True)
            return
        if path.exists() and planned.size is not None and path.stat().st_size == planned.size:
            self._report.skipped.append(planned.path)
            self._settled += 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._busy += 1
        self._fetch(planned, path)

    def _fetch(self, planned: PlannedFile, path: Path) -> None:
        downloader = QgsFileDownloader(QUrl(planned.url), str(path), "", True)
        # QgsFileDownloader deletes itself when it completes, fails, or is
        # cancelled. C++ must own it, or the Python wrapper deletes it again.
        sip.transferto(downloader, None)
        downloader.downloadCompleted.connect(
            lambda _url: self._download_ended(downloader, planned, path, None)
        )
        downloader.downloadError.connect(
            lambda errors: self._download_ended(downloader, planned, path, "; ".join(errors))
        )
        downloader.downloadCanceled.connect(
            lambda: self._download_ended(downloader, planned, path, "Cancelled")
        )
        self._downloaders.add(downloader)
        downloader.startDownload()

    def _download_ended(
        self,
        downloader: QgsFileDownloader,
        planned: PlannedFile,
        path: Path,
        error: str | None,
    ) -> None:
        # A cancelled QgsFileDownloader can emit downloadCanceled and
        # downloadError both. Only the first signal frees the slot.
        if downloader not in self._downloaders:
            return
        self._downloaders.discard(downloader)
        if error is None:
            self._fetched(planned, path)
        else:
            self._fail(planned, error)

    def _fetched(self, planned: PlannedFile, path: Path) -> None:
        self._report.downloaded.append(planned.path)
        if planned.checksum and not self._cancelled:
            self._verify(planned, path, existing=False)
        else:
            self._release(planned)

    def _verify(self, planned: PlannedFile, path: Path, *, existing: bool) -> None:
        checksum = planned.checksum or ""
        self.progress.emit(self._settled, self._total, f"Verifying {planned.path}")

        def hash_file(task: object) -> bool:
            cancelled = getattr(task, "isCanceled", lambda: False)
            return file_matches(path, checksum, cancelled=cancelled)

        def done(result: object, error: BaseException | None) -> None:
            self._tasks.discard(task)
            if self._cancelled:
                self._release(planned)
            elif isinstance(error, ChecksumError):
                # The catalog's checksum is unreadable, not the file. Keep it.
                if existing:
                    self._report.skipped.append(planned.path)
                self._release(planned)
            elif error is not None:
                self._fail(planned, f"Checksum check failed: {error}")
            elif result:
                self._report.verified += 1
                if existing:
                    self._report.skipped.append(planned.path)
                self._release(planned)
            elif existing:
                # A stale or partial copy. Replace it in the same slot.
                path.unlink(missing_ok=True)
                self._fetch(planned, path)
            else:
                path.unlink(missing_ok=True)
                self._fail(planned, "Downloaded bytes do not match file:checksum")

        task = run_task(f"Verify {planned.path}", hash_file, done)
        self._tasks.add(task)

    def _fail(self, planned: PlannedFile, reason: str) -> None:
        if not self._cancelled:
            self._report.failed.append((planned.path, reason))
        self._release(planned)

    def _release(self, planned: PlannedFile) -> None:
        self._busy -= 1
        self._settled += 1
        self.progress.emit(self._settled, self._total, planned.path)
        self._pump()
