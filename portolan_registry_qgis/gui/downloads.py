"""Download the selected files, or everything below a document in the area.

A download of everything first walks the catalog to list the files. The user
confirms the list, and then a download job fetches the files. With a map
view or an area, each GeoParquet file of the document downloads as a
GeoPackage of the features in the area.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox

from portolan_registry_qgis.core import download, parquet_query
from portolan_registry_qgis.core.i18n import _, ngettext, number
from portolan_registry_qgis.core.loadable import (
    parquet_name,
    parquet_to_clip,
    plan_summary,
    split_plan,
)
from portolan_registry_qgis.gui.loading import duckdb_ready
from portolan_registry_qgis.gui.tasks import Busy
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.network import fetch_json, run_task

if TYPE_CHECKING:
    from qgis.PyQt.QtWidgets import QWidget

    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Asset, Document
    from portolan_registry_qgis.gui.tasks import TaskController
    from portolan_registry_qgis.qgis_io.log import Messages


@dataclass(frozen=True)
class _Listed:
    """Where a download of the area goes, and the GeoParquet files it clips."""

    folder: Path
    root: str
    clip: list[Asset]
    limits: parquet_layer.Limits


class Downloads:
    """Ask where to download, list the files, and start the download job."""

    def __init__(self, parent: QWidget, messages: Messages, tasks: TaskController):
        self._parent = parent
        self._messages = messages
        self._tasks = tasks

    def _choose_folder(self) -> Path | None:
        folder = QFileDialog.getExistingDirectory(self._parent, _("Download into folder"))
        return Path(folder) if folder else None

    def download_files(self, root: str, tiles: list[str], assets: list[Asset]) -> None:
        """Download ``tiles`` and ``assets`` whole, to their paths below ``root``."""
        files = [
            download.PlannedFile(a.href, download.local_path(a.href, root), a.size, a.checksum)
            for a in assets
        ]
        files += [download.PlannedFile(url, download.local_path(url, root)) for url in tiles]
        if not files:
            return
        folder = self._choose_folder()
        if folder is not None:
            self._tasks.start_job(folder, files)

    def download_all(
        self, document: Document, root: str, scope: Scope, limits: parquet_layer.Limits
    ) -> None:
        """Download everything below ``document`` in ``scope``.

        The walk leaves out the items outside the scope. ``limits`` clips
        each GeoParquet file of the document.
        """
        clip = parquet_to_clip(document, scope)
        if clip and not duckdb_ready(self._parent):
            return
        folder = self._choose_folder()
        if folder is None:
            return
        title = document.title

        def build(task: Any, executor: Any) -> download.Plan:
            walk = download.Walk(cancelled=task.isCanceled, executor=executor)
            return download.plan(fetch_json, document.href, root, walk, scope=scope)

        def planned(result: object, error: BaseException | None, cancelled: bool) -> None:
            self._tasks.finish(Busy.LISTING)
            # A cancelled walk returns the partial plan it has. Drop it.
            if cancelled:
                self._messages.info(_("Listing of {title} cancelled.").format(title=title))
                return
            if not isinstance(result, download.Plan):
                self._messages.warn(
                    _("Could not list {title}: {error}").format(title=title, error=error)
                )
                return
            self._download_listed(result, _Listed(folder, root, clip, limits))

        # Translators: a task in the QGIS task manager that lists the files below {title}.
        description = _("List {title}").format(title=title)
        message = _("Listing everything below {title}…").format(title=title)
        self._tasks.run_walk(Busy.LISTING, description, message, build, planned)

    def _download_listed(self, plan: download.Plan, listed: _Listed) -> None:
        """Ask to download ``plan``, then download its files and clip the GeoParquet files."""
        whole = split_plan(plan, listed.clip)
        if not self._confirm(whole, len(listed.clip)):
            return
        if whole.files:
            self._tasks.start_job(listed.folder, whole.files)
        for asset in listed.clip:
            target = listed.folder / download.clipped_path(asset.href, listed.root)
            self._download_clipped(asset, target, listed.limits)

    def _confirm(self, plan: download.Plan, clipped: int = 0) -> bool:
        text = f"{plan_summary(plan, clipped)}\n\n{_('Download now?')}"
        # Translators: the title of the dialog that confirms a download.
        answer = QMessageBox.question(self._parent, _("Download"), text)
        return answer == QMessageBox.StandardButton.Yes

    def _download_clipped(self, asset: Asset, target: Path, limits: parquet_layer.Limits) -> None:
        """Write the features of a GeoParquet file in the scope to a GeoPackage at ``target``.

        A file with no geometry column has nothing to clip, so the copy
        holds every row.
        """
        extension_dir = parquet_layer.extension_directory()
        plan = parquet_layer.planner(asset.href, limits, extension_dir)
        name = parquet_name(asset)

        def write(task: Any) -> parquet_query.Written:
            planned = plan(task)
            prepared = parquet_layer.prepare(planned, extension_dir, cancelled=task.isCanceled)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(prepared.path), str(target))
            finally:
                parquet_layer.discard_path(prepared.path)
            return prepared.written

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_query.Written):
                if parquet_layer.is_interrupt(error):
                    self._messages.info(_("Stopped the download of {name}.").format(name=name))
                else:
                    self._messages.warn(
                        _("Could not write {path}: {error}").format(path=target, error=error)
                    )
                return
            wrote = ngettext(
                "Wrote {n} feature of {name} to {path}.",
                "Wrote {n} features of {name} to {path}.",
                result.features,
            )
            self._messages.info(wrote.format(n=number(result.features), name=name, path=target))

        self._messages.info(
            _("Writing the features of {name} in the chosen area to {path}…").format(
                name=name, path=target
            )
        )
        run_task(_("Write {name} in the chosen area").format(name=name), write, done)
