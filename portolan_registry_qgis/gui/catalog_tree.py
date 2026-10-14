"""The second page of the left pane: the tree of one publisher's catalog.

A collection whose children are all items shows one "N files" row.
"""

from __future__ import annotations

import contextlib
import html
from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QIcon, QPalette
from qgis.PyQt.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from portolan_registry_qgis.core.format import excerpt
from portolan_registry_qgis.core.i18n import _, count
from portolan_registry_qgis.core.loadable import ItemFiles, only_items
from portolan_registry_qgis.core.stac import Document, Node
from portolan_registry_qgis.gui import widgets
from portolan_registry_qgis.gui.pictures import ACCENT, pixmap
from portolan_registry_qgis.gui.widgets import ROLE, CatalogHeader, More, RoomyDelegate, theme_icon

if TYPE_CHECKING:
    from collections.abc import Callable

    from portolan_registry_qgis.core.registry import CatalogEntry
    from portolan_registry_qgis.gui.pictures import ImageCache, Picture, WorldMap
    from portolan_registry_qgis.qgis_io.documents import DocumentCache

    Opened = Callable[[object, BaseException | None], None]

_KIND_ICONS = {
    "catalog": "mIconFolder",
    "collection": "mIconLayerTree",
    "item": "mIconFile",
}
ICON_SIZE = QSize(18, 18)


def kind_icon(kind: str) -> QIcon:
    """Return the tree icon of a STAC ``kind``, such as "collection"."""
    return theme_icon(_KIND_ICONS.get(kind, _KIND_ICONS["item"]))


def _childless(item: QTreeWidgetItem) -> None:
    item.setChildIndicatorPolicy(
        QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
    )


class CatalogTree(QWidget):
    """Show the tree of one publisher's catalog, and emit the chosen node."""

    # Emits (href, title, item) when the current row is a node or an
    # "N files" row.
    node_chosen = pyqtSignal(str, str, object)

    def __init__(self, world: WorldMap, images: ImageCache, documents: DocumentCache):
        super().__init__()
        self._images = images
        self._documents = documents
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        nav = QHBoxLayout()
        nav.setContentsMargins(2, 2, 6, 0)
        self.back = QToolButton()
        self.back.setText(_("All publishers"))
        self.back.setArrowType(Qt.ArrowType.LeftArrow)
        self.back.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.back.setAutoRaise(True)
        nav.addWidget(self.back)
        nav.addStretch(1)
        layout.addLayout(nav)

        self.header = CatalogHeader(world)
        layout.addWidget(self.header)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.setIconSize(ICON_SIZE)
        self.tree.setItemDelegate(RoomyDelegate(self.tree))
        self.tree.setAnimated(True)
        layout.addWidget(self.tree, 1)
        self.tree.itemClicked.connect(self._tree_clicked)
        self.tree.itemActivated.connect(self._tree_clicked)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.currentItemChanged.connect(self._node_chosen)

    def show_entry(self, entry: CatalogEntry) -> None:
        """Show ``entry`` in the header, and empty the tree for its catalog."""
        self.header.show_entry(entry)
        self.tree.clear()

    def fill(self, parent: QTreeWidgetItem | None, document: Document) -> None:
        """List ``document``'s children under ``parent``, or at the top of the tree.

        When every child is an item, one row stands for all of them. A
        collection of many image tiles would otherwise fill the tree.
        """
        if not only_items(document):
            self._add_nodes(parent, document.children)
            return
        files = ItemFiles(document.href, document.title, len(document.children))
        row = QTreeWidgetItem([count("{n} file", "{n} files", files.count)])
        row.setData(0, ROLE, files)
        row.setIcon(0, kind_icon("item"))
        tip = _("Choose {title} to add or download these files")
        row.setToolTip(0, tip.format(title=html.escape(document.title)))
        if parent is None:
            self.tree.addTopLevelItem(row)
        else:
            parent.addChild(row)

    def _node_item(self, node: Node) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.title])
        item.setData(0, ROLE, node)
        item.setToolTip(0, node.href)
        item.setIcon(0, kind_icon(node.kind))
        if node.kind != "item":
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        return item

    def _add_nodes(self, parent: QTreeWidgetItem | None, nodes: tuple[Node, ...]) -> None:
        """Add one page of ``nodes``, and a row that lists the rest."""
        page = widgets.NODE_PAGE
        items = [self._node_item(node) for node in nodes[:page]]
        rest = nodes[page:]
        if rest:
            more = More(rest)
            row = QTreeWidgetItem([more.label(page)])
            row.setData(0, ROLE, more)
            row.setForeground(0, QBrush(ACCENT))
            row.setToolTip(
                0, count("This level lists {n} more", "This level lists {n} more", len(rest))
            )
            items.append(row)
        if parent is None:
            self.tree.addTopLevelItems(items)
        else:
            parent.addChildren(items)
        for item in items:
            node = item.data(0, ROLE)
            if isinstance(node, Node) and node.kind != "item":
                self._documents.open(node.href, self._decorate(item), quiet=True)

    def _tree_clicked(self, item: QTreeWidgetItem | None, _column: int = 0) -> None:
        if item is None or not isinstance(more := item.data(0, ROLE), More):
            return
        parent = item.parent()
        if parent is None:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        else:
            parent.removeChild(item)
        self._add_nodes(parent, more.rest)

    def _decorate(self, item: QTreeWidgetItem) -> Opened:
        """Return a callback that shows a read child's icon, kind, and summary."""

        def opened(document: object, _error: BaseException | None) -> None:
            if not isinstance(document, Document):
                return
            with contextlib.suppress(RuntimeError):
                item.setIcon(0, kind_icon(document.kind))
                if document.description:
                    item.setToolTip(0, excerpt(document.description))
                if not document.children:
                    _childless(item)
            if document.icon:
                self._images.request(document.icon, self._tree_icon(item))

        return opened

    def _tree_icon(self, item: QTreeWidgetItem) -> Callable[[Picture | None], None]:
        def show(picture: Picture | None) -> None:
            if picture is None:
                return
            text = self.tree.palette().color(QPalette.ColorRole.Text)
            icon = QIcon(pixmap(picture, ICON_SIZE, text, self.tree.devicePixelRatioF()))
            with contextlib.suppress(RuntimeError):
                item.setIcon(0, icon)

        return show

    def _expand(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, ROLE)
        if not isinstance(node, Node) or item.data(1, ROLE):
            return
        item.setData(1, ROLE, True)
        loading = QTreeWidgetItem([_("Reading…")])
        loading.setFlags(Qt.ItemFlag.NoItemFlags)
        item.addChild(loading)

        def opened(document: object, error: BaseException | None) -> None:
            item.takeChildren()
            if not isinstance(document, Document):
                item.setData(1, ROLE, False)
                failed = QTreeWidgetItem([_("Could not read: {error}").format(error=error)])
                failed.setFlags(Qt.ItemFlag.NoItemFlags)
                item.addChild(failed)
                return
            self.fill(item, document)
            if not document.children:
                _childless(item)

        self._documents.open(node.href, opened)

    def _node_chosen(self, item: QTreeWidgetItem | None, _previous: object = None) -> None:
        node = item.data(0, ROLE) if item is not None else None
        if isinstance(node, (Node, ItemFiles)):
            self.node_chosen.emit(node.href, node.title, item)
