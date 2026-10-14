"""Add files to the map: vector tiles, single files, GeoParquet copies, and mosaics."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from qgis.core import Qgis
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QImage
from qgis.PyQt.QtWidgets import QMessageBox

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.i18n import _, count, ngettext, number
from portolan_registry_qgis.core.loadable import parquet_name
from portolan_registry_qgis.core.scope import Everything, clip_wkt
from portolan_registry_qgis.gui.tasks import Busy
from portolan_registry_qgis.qgis_io import layers as layer_io
from portolan_registry_qgis.qgis_io import mosaic, parquet_layer
from portolan_registry_qgis.qgis_io.canvas import add_layers
from portolan_registry_qgis.qgis_io.log import log
from portolan_registry_qgis.qgis_io.network import fetch_bytes, fetch_json, run_task

if TYPE_CHECKING:
    from pathlib import Path

    from qgis.PyQt.QtWidgets import QWidget

    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Asset, Document
    from portolan_registry_qgis.gui.tasks import TaskController
    from portolan_registry_qgis.qgis_io.log import Messages
    from portolan_registry_qgis.qgis_io.tileserver import TileServer

# A GeoParquet load above this many features asks first. A load of any size
# works, but it takes time and disk space.
CONFIRM_FEATURES = 1_000_000
# A mosaic of more than this many files asks first. GDAL opens each file to
# write the VRT.
CONFIRM_FILES = 500


def zoomed(message: str, *, moved: bool) -> str:
    """Return ``message``, and the note that the map moved when ``moved`` is True."""
    return f"{message} {_('The map moved to show it.')}" if moved else message


def duckdb_ready(parent: QWidget) -> bool:
    """Return whether DuckDB can read GeoParquet. If not, say how to install it."""
    ok, version = parquet_query.duckdb_status()
    if not ok:
        _duckdb_help(parent, version)
    return ok


def _duckdb_help(parent: QWidget, version: str | None) -> None:
    minimum = ".".join(str(part) for part in parquet_query.MINIMUM_DUCKDB)
    if version:
        heading = _("DuckDB {version} is too old. GeoParquet needs {minimum} or newer.").format(
            version=html.escape(version), minimum=minimum
        )
    else:
        heading = _("GeoParquet needs DuckDB {minimum} or newer, and it is not installed.").format(
            minimum=minimum
        )
    command = html.escape(parquet_query.install_command(upgrade=version is not None))
    where = (
        _("QGIS runs in a Flatpak, so install DuckDB into your user folder from a terminal:")
        if parquet_query.is_flatpak()
        else _("Install it from a terminal with the Python that QGIS uses:")
    )
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(_("Portolan Registry: DuckDB needed"))
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(
        f"<p><b>{heading}</b></p><p>{where}</p><pre>{command}</pre><p>{_('Then restart QGIS.')}</p>"
    )
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    box.exec()


def _added_rows(planned: parquet_layer.Planned, features: int) -> str:
    if planned.plan.has_geometry:
        return ngettext(
            "Added {n} feature from {name}.", "Added {n} features from {name}.", features
        )
    return ngettext("Added {n} row from {name}.", "Added {n} rows from {name}.", features)


def _no_geometry(prepared: parquet_layer.Prepared, refused: int) -> str:
    # Translators: {geometry_type} is a geometry type, such as Point or Polygon.
    no_geometry = ngettext(
        "{n} of them has no geometry, because its geometry type "
        "does not fit a {geometry_type} layer.",
        "{n} of them have no geometry, because their geometry type "
        "does not fit a {geometry_type} layer.",
        refused,
    )
    return no_geometry.format(n=number(refused), geometry_type=prepared.written.geometry_type)


def _copying(planned: parquet_layer.Planned, name: str) -> str:
    if planned.estimate is None:
        return _("Copying the features of {name} with DuckDB…").format(name=name)
    return ngettext(
        "Copying about {n} feature of {name} with DuckDB…",
        "Copying about {n} features of {name} with DuckDB…",
        planned.estimate,
    ).format(n=number(planned.estimate), name=name)


class Loader:
    """Add the files of a document to the map, and say what happened."""

    def __init__(
        self,
        parent: QWidget,
        iface: Any,
        server: TileServer,
        messages: Messages,
        tasks: TaskController,
    ):
        self._parent = parent
        self._iface = iface
        self._server = server
        self._messages = messages
        self._tasks = tasks

    def _add_layers(self, layers: list[Any]) -> bool:
        return add_layers(self._iface.mapCanvas(), layers)

    # ---------- tiles and single files ----------

    def add_tiles(self, document: Document, urls: list[str]) -> None:
        """Add the PMTiles archives ``urls`` of ``document``, with its styles."""
        picked = list(dict.fromkeys(urls))

        def prepare(_task: object) -> layer_io.PreparedTiles:
            return layer_io.prepare_pmtiles(document, picked, fetch_bytes)

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, layer_io.PreparedTiles):
                self._messages.warn(_("Could not add vector tiles: {error}").format(error=error))
                return
            try:
                layers, warnings = layer_io.build_pmtiles(self._server, result, QImage.fromData)
            except layer_io.LayerError as failure:
                self._messages.warn(str(failure))
                return
            for note in warnings:
                log(note, Qgis.MessageLevel.Warning)
            moved = self._add_layers(layers)
            added = ngettext(
                "Added {n} vector tile layer from {title}.",
                "Added {n} vector tile layers from {title}.",
                len(layers),
            )
            self._messages.info(
                zoomed(added.format(n=number(len(layers)), title=document.title), moved=moved)
            )

        run_task(_("Open vector tiles of {title}").format(title=document.title), prepare, done)

    def add_asset(self, asset: Asset) -> None:
        """Add ``asset`` whole, read in place."""
        try:
            layer = layer_io.asset_layer(asset)
        except layer_io.LayerError as failure:
            self._messages.warn(str(failure))
            return
        moved = self._add_layers([layer])
        self._messages.info(zoomed(_("Added {name}.").format(name=asset.label), moved=moved))

    # ---------- GeoParquet ----------

    def add_parquet(
        self, asset: Asset, scope: Scope, limits: parquet_layer.Limits, *, has_tiles: bool
    ) -> None:
        """Copy the features of ``asset`` within ``limits`` to a layer.

        ``scope`` says whether an area limits the read. ``has_tiles`` adds
        the advice to view the vector tiles to the question about a large
        load. Call ``duckdb_ready`` first.
        """
        extension_dir = parquet_layer.extension_directory()
        read = parquet_layer.planner(asset.href, limits, extension_dir)
        name = parquet_name(asset)
        in_scope = not isinstance(scope, Everything)

        def planned(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_layer.Planned):
                if parquet_layer.is_interrupt(error):
                    self._messages.info(_("Stopped reading {name}.").format(name=name))
                else:
                    self._messages.warn(
                        _("DuckDB could not read {url}: {error}").format(
                            url=asset.href, error=error
                        )
                    )
                return
            # A table has no geometry, so the scope does not limit it.
            limited = in_scope and result.plan.has_geometry
            estimate = result.estimate or 0
            if estimate > CONFIRM_FEATURES and not self._confirm_features(
                name, estimate, in_scope=limited, has_tiles=has_tiles
            ):
                self._messages.info(_("Did not load {name}.").format(name=name))
                return
            self._copy_parquet(result, name, extension_dir, in_scope=limited)

        self._messages.info(_("Reading {name} with DuckDB…").format(name=name))
        run_task(_("Read {url} with DuckDB").format(url=asset.href), read, planned)

    def _confirm_features(
        self, name: str, estimate: int, *, in_scope: bool, has_tiles: bool
    ) -> bool:
        if in_scope:
            holds = ngettext(
                "{name} holds about {n} feature in the chosen area. DuckDB copies them to a "
                "GeoPackage on disk, which takes time and disk space.",
                "{name} holds about {n} features in the chosen area. DuckDB copies them to a "
                "GeoPackage on disk, which takes time and disk space.",
                estimate,
            )
        else:
            holds = ngettext(
                "{name} holds about {n} feature in the file. DuckDB copies them to a "
                "GeoPackage on disk, which takes time and disk space.",
                "{name} holds about {n} features in the file. DuckDB copies them to a "
                "GeoPackage on disk, which takes time and disk space.",
                estimate,
            )
        paragraphs = [holds.format(name=name, n=number(estimate))]
        if has_tiles:
            paragraphs.append(_("For a quick look, use View on map instead."))
        paragraphs.append(_("Load the features?"))
        answer = QMessageBox.question(self._parent, _("Load GeoParquet"), "\n\n".join(paragraphs))
        return answer == QMessageBox.StandardButton.Yes

    def _copy_parquet(
        self, planned: parquet_layer.Planned, name: str, extension_dir: str, *, in_scope: bool
    ) -> None:
        def copy(task: Any) -> parquet_layer.Prepared:
            return parquet_layer.prepare(planned, extension_dir, cancelled=task.isCanceled)

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_layer.Prepared):
                if parquet_layer.is_interrupt(error):
                    self._messages.info(_("Stopped the copy of {name}.").format(name=name))
                else:
                    self._messages.warn(
                        _("DuckDB could not read {url}: {error}").format(
                            url=planned.url, error=error
                        )
                    )
                return
            self._show_copy(planned, result, name, in_scope=in_scope)

        self._messages.info(_copying(planned, name))
        run_task(_("Copy {url} with DuckDB").format(url=planned.url), copy, done)

    def _show_copy(
        self,
        planned: parquet_layer.Planned,
        prepared: parquet_layer.Prepared,
        name: str,
        *,
        in_scope: bool,
    ) -> None:
        """Add the layer of a finished copy, and say how many features it holds."""
        features = prepared.written.features
        if not features:
            parquet_layer.discard_path(prepared.path)
            self._messages.info(
                _("No feature intersects the chosen area.")
                if in_scope
                else _("{name} is empty.").format(name=name)
            )
            return
        try:
            layer, refused = parquet_layer.build(prepared, name)
        except OSError as failure:
            self._messages.warn(str(failure))
            return
        moved = self._add_layers([layer])
        notes = [
            zoomed(
                _added_rows(planned, features).format(n=number(features), name=name), moved=moved
            )
        ]
        if refused:
            notes.append(_no_geometry(prepared, refused))
        (self._messages.warn if refused else self._messages.info)(" ".join(notes))

    # ---------- mosaics ----------

    def add_mosaic(self, document: Document, scope: Scope) -> None:
        """Add the COG files of the items of ``document`` in ``scope`` as one layer."""
        title = document.title

        def find(task: Any, executor: Any) -> list[str]:
            return mosaic.item_hrefs_in(
                document, scope, fetch_json, executor, cancelled=task.isCanceled
            )

        def found(result: object, error: BaseException | None, *, cancelled: bool) -> None:
            hrefs = self._mosaic_files(result, error, title, cancelled=cancelled)
            if hrefs is None:
                self._tasks.finish(Busy.MOSAIC)
                return
            self._build_mosaic(hrefs, title, clip_wkt(scope))

        self._tasks.run_walk(
            Busy.MOSAIC,
            _("List the files of {title}").format(title=title),
            _("Finding the files of {title} in the area…").format(title=title),
            find,
            found,
        )

    def _mosaic_files(
        self, result: object, error: BaseException | None, title: str, *, cancelled: bool
    ) -> list[str] | None:
        """Return the files of a mosaic search, or None when the mosaic stops.

        The method says why the mosaic stops.
        """
        # A cancelled walk returns the partial list it has. Drop it.
        if cancelled:
            self._messages.info(
                _("Stopped the search for the files of {title}.").format(title=title)
            )
            return None
        if not isinstance(result, list):
            self._messages.warn(
                _("Could not list the files of {title}: {error}").format(title=title, error=error)
            )
            return None
        if not result:
            self._messages.info(
                _("No COG file of {title} lies in the chosen area.").format(title=title)
            )
            return None
        if len(result) > CONFIRM_FILES and not self._confirm_mosaic(title, len(result)):
            self._messages.info(_("Did not add {title}.").format(title=title))
            return None
        return result

    def _confirm_mosaic(self, title: str, files: int) -> bool:
        lie = ngettext(
            "{n} file of {title} lies in the chosen area. GDAL opens each "
            "file to build the mosaic, which takes time.",
            "{n} files of {title} lie in the chosen area. GDAL opens each "
            "file to build the mosaic, which takes time.",
            files,
        )
        paragraphs = [
            lie.format(n=number(files), title=title),
            _("To add fewer files, draw a smaller area."),
            _("Add the mosaic?"),
        ]
        answer = QMessageBox.question(self._parent, _("Add as one layer"), "\n\n".join(paragraphs))
        return answer == QMessageBox.StandardButton.Yes

    def _build_mosaic(self, hrefs: list[str], name: str, clip: str | None = None) -> None:
        def done(result: Path | None, error: BaseException | None) -> None:
            self._tasks.hide_progress()
            self._tasks.finish(Busy.MOSAIC)
            if result is None:
                if isinstance(error, mosaic.MosaicCancelledError):
                    self._messages.info(_("Stopped the mosaic of {name}.").format(name=name))
                else:
                    self._messages.warn(
                        _("Could not build the mosaic of {name}: {error}").format(
                            name=name, error=error
                        )
                    )
                return
            try:
                layer = mosaic.open_mosaic(result, hrefs, name, clip)
            except layer_io.LayerError as failure:
                self._messages.warn(str(failure))
                return
            moved = self._add_layers([layer])
            added = ngettext(
                "Added {n} file of {name} as one layer.",
                "Added {n} files of {name} as one layer.",
                len(hrefs),
            )
            self._messages.info(zoomed(added.format(n=number(len(hrefs)), name=name), moved=moved))

        self._tasks.show_progress(
            0,
            0,
            count("Building a mosaic of {n} file…", "Building a mosaic of {n} files…", len(hrefs)),
        )
        description = _("Build a mosaic of {name}").format(name=name)
        self._tasks.track(mosaic.start_vrt(description, hrefs, clip, done))
