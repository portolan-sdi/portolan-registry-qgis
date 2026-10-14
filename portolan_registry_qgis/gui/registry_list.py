"""The first page of the left pane: the registry's publishers.

A search box and an "In map extent" check box filter the list. Each row
shows the publisher's map of its extent and its logo. A click emits
``catalog_chosen`` with the publisher's ``CatalogEntry``.
"""

from __future__ import annotations

import contextlib
import html
from typing import TYPE_CHECKING, Any

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from portolan_registry_qgis.core import registry
from portolan_registry_qgis.core.i18n import _, count, ngettext, number
from portolan_registry_qgis.gui.pictures import muted
from portolan_registry_qgis.gui.widgets import ROLE, CatalogDelegate, More, open_label, open_url
from portolan_registry_qgis.qgis_io.canvas import canvas_bbox
from portolan_registry_qgis.qgis_io.network import fetch_json, run_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from portolan_registry_qgis.core.registry import CatalogEntry
    from portolan_registry_qgis.gui.pictures import ImageCache, WorldMap
    from portolan_registry_qgis.qgis_io.log import Messages

# The list shows this many rows, then a row that shows the next page.
CATALOG_PAGE = 25


class RegistryList(QWidget):
    """List the registry's publishers, and emit the one the user chooses."""

    # Emits the CatalogEntry of the publisher the user chooses.
    catalog_chosen = pyqtSignal(object)

    def __init__(
        self,
        iface: Any,
        images: ImageCache,
        world: WorldMap,
        messages: Messages,
        browser_link: Callable[[str | None], str | None],
    ):
        super().__init__()
        self._iface = iface
        self._messages = messages
        self._browser_link = browser_link
        self._entries: list[CatalogEntry] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 0, 0)
        layout.setSpacing(6)
        self.search = QLineEdit(placeholderText=_("Search publishers by name or license"))
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.in_extent = QCheckBox(_("In map extent"))
        self.in_extent.setToolTip(_("Show only publishers whose data overlaps the map"))
        self.shown = QLabel()
        filters.addWidget(self.in_extent)
        filters.addStretch(1)
        filters.addWidget(self.shown)
        layout.addLayout(filters)

        self.catalogs = QListWidget()
        self.catalogs.setFrameShape(QFrame.Shape.NoFrame)
        self.catalogs.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.catalogs.setMouseTracking(True)
        # Rows grow when a title wraps, so they follow the panel's width.
        self.catalogs.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.catalogs.setItemDelegate(CatalogDelegate(self.catalogs, images, world))
        self.catalogs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        layout.addWidget(self.catalogs, 1)
        self._connect()

    def _connect(self) -> None:
        self.search.textChanged.connect(self.show_catalogs)
        self.in_extent.toggled.connect(self.show_catalogs)
        self._iface.mapCanvas().extentsChanged.connect(self._extent_changed)
        self.catalogs.itemClicked.connect(self._chosen)
        self.catalogs.itemActivated.connect(self._chosen)
        self.catalogs.customContextMenuRequested.connect(self._context)

    def release(self) -> None:
        """Stop following the map canvas. Call before the list is deleted."""
        with contextlib.suppress(TypeError):
            self._iface.mapCanvas().extentsChanged.disconnect(self._extent_changed)

    @property
    def entries(self) -> list[CatalogEntry]:
        """Return the publishers that the registry lists."""
        return self._entries

    @entries.setter
    def entries(self, entries: list[CatalogEntry]) -> None:
        """List ``entries`` as the registry's publishers."""
        self._entries = entries
        self.show_catalogs()

    def reload(self, url: str) -> None:
        """Read the registry at ``url`` and list its publishers."""
        self.catalogs.clear()
        self.shown.clear()
        self._placeholder(_("Reading the Portolan registry…"))

        def read(_task: object) -> list[CatalogEntry]:
            return registry.parse_registry(fetch_json(url), url)

        run_task(_("Read the Portolan registry"), read, self._registry_read)

    def _registry_read(self, result: object, error: BaseException | None) -> None:
        if error is not None or not isinstance(result, list):
            self.catalogs.clear()
            self._placeholder(
                _("Could not read the Portolan registry. Check the connection, then reload.")
            )
            self._messages.warn(
                _("Could not read the Portolan registry: {error}").format(error=error)
            )
            return
        self.entries = result

    def _extent_changed(self) -> None:
        if self.in_extent.isChecked():
            self.show_catalogs()

    def show_catalogs(self) -> None:
        """List the publishers that pass the search and the extent filter."""
        bbox = canvas_bbox(self._iface.mapCanvas()) if self.in_extent.isChecked() else None
        shown = registry.filter_entries(self._entries, self.search.text(), bbox=bbox)
        self.catalogs.clear()
        self._list_catalogs(shown)
        total = len(self._entries)
        if len(shown) == total:
            self.shown.setText(count("{n} publisher", "{n} publishers", total))
        else:
            # Translators: {shown} publishers pass the filters, of {n} in the registry.
            of_total = ngettext("{shown} of {n} publisher", "{shown} of {n} publishers", total)
            self.shown.setText(of_total.format(shown=number(len(shown)), n=number(total)))
        self.shown.setStyleSheet(f"color: {muted(self.palette()).name()};")
        if not shown and self._entries:
            self._placeholder(_("No publisher matches these filters."))

    def _list_catalogs(self, entries: list[CatalogEntry]) -> None:
        """Add one page of ``entries``, and a row that lists the rest."""
        for entry in entries[:CATALOG_PAGE]:
            item = QListWidgetItem(entry.title)
            item.setData(ROLE, entry)
            tip = [html.escape(entry.title), html.escape(entry.url)]
            if entry.failure_reason:
                tip.append(html.escape(entry.failure_reason))
            item.setToolTip("<br>".join(tip))
            self.catalogs.addItem(item)
        rest = entries[CATALOG_PAGE:]
        if rest:
            more = More(tuple(rest))
            item = QListWidgetItem(more.label(CATALOG_PAGE, "publisher"))
            item.setData(ROLE, more)
            self.catalogs.addItem(item)

    def _placeholder(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.catalogs.addItem(item)

    def _chosen(self, item: QListWidgetItem | None) -> None:
        entry = item.data(ROLE) if item is not None else None
        if isinstance(entry, More):
            self.catalogs.takeItem(self.catalogs.row(item))
            self._list_catalogs(list(entry.rest))
        elif isinstance(entry, registry.CatalogEntry):
            self.catalog_chosen.emit(entry)

    def catalog_menu(self, entry: CatalogEntry) -> QMenu:
        """Return the context menu of a publisher row."""
        menu = QMenu(self.catalogs)
        action = menu.addAction(open_label())
        link = self._browser_link(entry.url)
        action.setEnabled(link is not None)
        if link is not None:
            action.setToolTip(link)
            action.triggered.connect(lambda: open_url(link))
        return menu

    def _context(self, position: Any) -> None:
        item = self.catalogs.itemAt(position)
        entry = item.data(ROLE) if item is not None else None
        if isinstance(entry, registry.CatalogEntry):
            self.catalog_menu(entry).exec(self.catalogs.viewport().mapToGlobal(position))
