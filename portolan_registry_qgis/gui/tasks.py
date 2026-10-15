"""The progress bar and Cancel button that the window's long tasks share.

Only one task runs at a time. ``busy_changed`` tells the window to sync its buttons.
"""

from __future__ import annotations

import time
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from qgis.core import Qgis
from qgis.PyQt.QtCore import pyqtSignal, pyqtSlot
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout

from portolan_registry_qgis.core import download
from portolan_registry_qgis.core.i18n import _, count
from portolan_registry_qgis.core.progress import Clock, progress_text
from portolan_registry_qgis.qgis_io.downloader import DownloadJob
from portolan_registry_qgis.qgis_io.log import log
from portolan_registry_qgis.qgis_io.network import QtExecutor, run_task

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from qgis.core import QgsTask

    from portolan_registry_qgis.qgis_io.downloader import DownloadReport
    from portolan_registry_qgis.qgis_io.log import Messages

    Walk = Callable[[QgsTask, QtExecutor], object]


class Walked(Protocol):
    """The function that receives the end of a walk on the main thread."""

    def __call__(self, result: object, error: BaseException | None, *, cancelled: bool) -> None:
        """Receive the result, the error, and whether the user cancelled the walk."""


class Busy(Enum):
    """A walk that keeps the window busy until it ends."""

    MOSAIC = auto()
    LISTING = auto()


class TaskController(QFrame):
    """Run one long task at a time, and show its progress with Cancel."""

    # Emits when a task starts or ends.
    busy_changed = pyqtSignal()

    def __init__(self, messages: Messages, parent: QFrame | None = None):
        super().__init__(parent)
        self._messages = messages
        self._job: DownloadJob | None = None
        self._busy: set[Busy] = set()
        # The task of the mosaic search, the mosaic build, or the listing.
        # Cancel stops it.
        self._task: QgsTask | None = None
        # The time left of the task the bar shows, and what the bar says.
        self._clock = Clock()
        self._message = ""
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.hide()
        self.cancel = QPushButton(_("Cancel"))
        self.cancel.hide()
        self.cancel.clicked.connect(self.stop)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel)
        layout.addLayout(progress_row)
        self.hide()

    def is_busy(self) -> bool:
        """Return whether a mosaic search, a download listing, or a download runs."""
        return self._job is not None or bool(self._busy)

    def refuse_when_busy(self) -> bool:
        """Say why an action waits, and return True, while another task runs."""
        if not self.is_busy():
            return False
        if Busy.MOSAIC in self._busy:
            self._messages.info(_("Wait for the mosaic to finish, or cancel it."))
        elif Busy.LISTING in self._busy:
            self._messages.info(
                _("Wait for the list of files to download to finish, or cancel it.")
            )
        else:
            self._messages.info(_("Wait for the download to finish, or cancel it."))
        return True

    def begin(self, busy: Busy) -> None:
        """Mark the window busy with ``busy``."""
        self._busy.add(busy)
        self.busy_changed.emit()

    def finish(self, busy: Busy) -> None:
        """Mark the end of ``busy``. A second call does no harm."""
        self._busy.discard(busy)
        self.busy_changed.emit()

    def run_walk(
        self, busy: Busy, description: str, message: str, walk: Walk, on_done: Walked
    ) -> None:
        """Walk a catalog in a background task, with a pool of reader threads.

        The window stays busy with ``busy`` until ``on_done`` calls ``finish``,
        or until ``on_done`` raises.
        """
        self.begin(busy)
        self._clock = Clock()
        self.show_progress(0, 0, message)

        def work(task: QgsTask) -> object:
            executor = QtExecutor(download.WALK_WORKERS)
            try:
                return walk(task, executor)
            finally:
                executor.shutdown(cancel_futures=True)

        def done(result: object, error: BaseException | None) -> None:
            # ``task`` is bound below, before the walk can end.
            try:
                self.hide_progress()
                on_done(result, error, cancelled=task.isCanceled())
            except BaseException:
                # The flag must not stay set, or every later click does nothing.
                self.finish(busy)
                raise

        task = run_task(description, work, done)
        self.track(task)

    def start_job(self, folder: Path, files: list[download.PlannedFile]) -> None:
        """Download ``files`` into ``folder``, and report when the job ends."""
        job = DownloadJob(folder, files, self)
        self._clock = Clock()
        job.progress.connect(
            lambda done, total, message: self.show_fraction(
                job.fraction, f"{message}  ({done}/{total})"
            )
        )
        job.finished.connect(lambda report: self._downloaded(folder, report))
        self._job = job
        self.cancel.show()
        self.busy_changed.emit()
        job.start()

    def track(self, task: QgsTask) -> None:
        """Let Cancel stop ``task``, a search, a listing, or a mosaic build."""
        self._task = task
        self.cancel.show()

    def follow(self, task: QgsTask, message: str) -> None:
        """Show the progress of ``task`` with ``message`` and the time left, and let Cancel stop it.

        A task that starts while another holds the bar shows in the QGIS task manager only.
        """
        if self._job is not None or (self._task is not None and self._task is not task):
            return
        self._clock = Clock()
        self._message = message
        self.show_fraction(None, message)
        self.track(task)
        task.progressChanged.connect(self._task_progress)

    def release(self, task: QgsTask) -> None:
        """Hide the bar when it still shows ``task``."""
        if self._task is task:
            self.hide_progress()

    @pyqtSlot(float)
    def _task_progress(self, percent: float) -> None:
        if self._task is not None and self.sender() is self._task:
            self.show_fraction(percent / 100, self._message)

    def show_fraction(self, fraction: float | None, message: str) -> None:
        """Show ``message``, with the share done and the time left when ``fraction`` is known."""
        scale = 1000
        self.progress.setRange(0, scale if fraction is not None else 0)
        left = None
        if fraction is not None:
            fraction = self._clock.share(fraction)
            left = self._clock.left(fraction, time.monotonic())
            self.progress.setValue(int(fraction * scale))
        self.progress.setFormat(progress_text(message, fraction, left))
        self.progress.show()
        self.show()

    def stop(self) -> None:
        """Stop the download, or the task that Cancel tracks."""
        if self._job is not None:
            self._job.cancel()
        elif self._task is not None:
            self._task.cancel()

    def show_progress(self, done: int, total: int, message: str) -> None:
        """Show ``message``, and ``done`` of ``total`` when the total is known."""
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress.setFormat(f"{message}  (%v/%m)" if total else message)
        self.progress.show()
        self.show()

    def hide_progress(self) -> None:
        """Hide the bar, and forget the task that Cancel tracks."""
        self._task = None
        self.progress.hide()
        self.cancel.hide()
        self.hide()

    def _downloaded(self, folder: Path, report: DownloadReport) -> None:
        self._job = None
        self.hide_progress()
        self.busy_changed.emit()
        for path, reason in report.failed:
            log(f"{path}: {reason}", Qgis.MessageLevel.Warning)
        summary = ", ".join(
            (
                count("{n} downloaded", "{n} downloaded", len(report.downloaded)),
                count("{n} already present", "{n} already present", len(report.skipped)),
                count("{n} checksum verified", "{n} checksums verified", report.verified),
                count("{n} failed", "{n} failed", len(report.failed)),
            )
        )
        if report.cancelled:
            self._messages.warn(_("Download cancelled: {summary}.").format(summary=summary))
        elif report.failed:
            self._messages.warn(
                _("Download into {folder} finished with errors: {summary}.").format(
                    folder=folder, summary=summary
                )
            )
        else:
            self._messages.info(
                _("Download into {folder} finished: {summary}.").format(
                    folder=folder, summary=summary
                )
            )
