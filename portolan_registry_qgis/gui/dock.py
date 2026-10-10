"""The Portolan Registry dock panel.

The panel has two pages. The first lists the registry's catalogs, each with
a map of its extent and its logo. A click opens the second page, which shows
the catalog's tree and the chosen node's picture, details, and assets, with
the actions that add them to the map or download them.
"""

from __future__ import annotations

import contextlib
import html
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QImage, QPalette
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from portolan_registry_qgis.core import download, parquet_query, registry
from portolan_registry_qgis.core.format import count, format_label, human_size
from portolan_registry_qgis.core.stac import Asset, Document, Node, read_document
from portolan_registry_qgis.gui.pictures import ACCENT, ImageCache, WorldMap, mix, muted, pixmap
from portolan_registry_qgis.gui.widgets import (
    BADGE_ROLE,
    ROLE,
    AssetDelegate,
    CatalogDelegate,
    CatalogHeader,
    HeroView,
    More,
    RoomyDelegate,
)
from portolan_registry_qgis.qgis_io import layers as layer_io
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.downloader import DownloadJob, DownloadReport
from portolan_registry_qgis.qgis_io.network import (
    QtExecutor,
    fetch_bytes,
    fetch_json,
    run_task,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from portolan_registry_qgis.core.registry import CatalogEntry
    from portolan_registry_qgis.core.stac import Bbox
    from portolan_registry_qgis.gui.pictures import Picture
    from portolan_registry_qgis.qgis_io.tileserver import TileServer

LOG_TAG = "Portolan Registry"
_ROLE = ROLE
_WGS84 = "EPSG:4326"
_PMTILES_KEY = "\x00pmtiles:"
# The lists show this many rows, then a row that shows the next page. The
# tree reads the child documents of each page it shows, for their icons.
CATALOG_PAGE = 25
NODE_PAGE = 50
# A GeoParquet load above this many features asks first. A load of any size
# works, but it takes time and disk space.
CONFIRM_FEATURES = 1_000_000
VIEW_OR_ANALYZE = "For quick viewing, add the PMTiles. For analysis, load the GeoParquet."
_KIND_ICONS = {
    "catalog": "/mIconFolder.svg",
    "collection": "/mIconLayerTree.svg",
    "item": "/mIconFile.svg",
}
_ICON_SIZE = QSize(18, 18)
_REGISTRY_PAGE, _CATALOG_PAGE = 0, 1


def _log(message: str, level: Any = None) -> None:
    QgsMessageLog.logMessage(message, LOG_TAG, level or Qgis.MessageLevel.Info)


def _kind_icon(kind: str) -> QIcon:
    return QgsApplication.getThemeIcon(_KIND_ICONS.get(kind, _KIND_ICONS["item"]))


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


class RegistryDock(QDockWidget):
    """Browse the Portolan registry, add its data to the map, and download it."""

    def __init__(self, iface: Any, server: TileServer, registry_url: str = registry.REGISTRY_URL):
        super().__init__("Portolan Registry")
        self.setObjectName("PortolanRegistryDock")
        self._iface = iface
        self._server = server
        self._registry_url = registry_url
        self._entries: list[CatalogEntry] = []
        self._catalog: CatalogEntry | None = None
        self._documents: dict[str, Document] = {}
        self._reading: dict[str, list[Callable[[object, BaseException | None], None]]] = {}
        self._document: Document | None = None
        self._job: DownloadJob | None = None
        self.images = ImageCache(self)
        self.world = WorldMap(self)
        self._build()
        self.reload()

    # ---------- layout ----------

    def _build(self) -> None:
        self.pages = QStackedWidget()
        self.pages.addWidget(self._registry_page())
        self.pages.addWidget(self._catalog_page())
        self.setWidget(self.pages)
        self._connect()
        self._sync_buttons()

    def _registry_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.setSpacing(6)
        self.search = QLineEdit(placeholderText="Search by name, ID, or license")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.status = QComboBox()
        self.status.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.in_extent = QCheckBox("In map extent")
        self.in_extent.setToolTip("Show only catalogs whose extent overlaps the map")
        self.shown = QLabel()
        self.refresh = QToolButton()
        self.refresh.setIcon(QgsApplication.getThemeIcon("/mActionRefresh.svg"))
        self.refresh.setAutoRaise(True)
        self.refresh.setToolTip("Read the registry again")
        filters.addWidget(self.status)
        filters.addWidget(self.in_extent)
        filters.addStretch(1)
        filters.addWidget(self.shown)
        filters.addWidget(self.refresh)
        layout.addLayout(filters)

        self.catalogs = QListWidget()
        self.catalogs.setFrameShape(QFrame.Shape.NoFrame)
        self.catalogs.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.catalogs.setMouseTracking(True)
        # Rows grow when a title wraps, so they follow the panel's width.
        self.catalogs.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.catalogs.setItemDelegate(CatalogDelegate(self.catalogs, self.images, self.world))
        layout.addWidget(self.catalogs, 1)
        return page

    def _catalog_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        nav = QHBoxLayout()
        nav.setContentsMargins(2, 2, 6, 0)
        self.back = QToolButton()
        self.back.setText("All catalogs")
        self.back.setArrowType(Qt.ArrowType.LeftArrow)
        self.back.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.back.setAutoRaise(True)
        nav.addWidget(self.back)
        nav.addStretch(1)
        layout.addLayout(nav)

        self.header = CatalogHeader(self.world)
        layout.addWidget(self.header)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.setIconSize(_ICON_SIZE)
        self.tree.setItemDelegate(RoomyDelegate(self.tree))
        self.tree.setAnimated(True)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.tree)
        splitter.addWidget(self._details_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        layout.addWidget(self._action_bar())
        return page

    def _details_panel(self) -> QScrollArea:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.hero = HeroView(self.world)
        layout.addWidget(self.hero)

        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.title.setFont(font)
        layout.addWidget(self.title)

        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setTextFormat(Qt.TextFormat.MarkdownText)
        self.description.setOpenExternalLinks(True)
        self.description.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(self.description)

        self.facts = QLabel()
        self.facts.setWordWrap(True)
        self.facts.setTextFormat(Qt.TextFormat.RichText)
        self.facts.setOpenExternalLinks(True)
        self.facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(self.facts)

        self.assets_heading = _heading("Assets")
        layout.addSpacing(4)
        layout.addWidget(self.assets_heading)
        self.assets = QTreeWidget()
        self.assets.setHeaderHidden(True)
        self.assets.setRootIsDecorated(False)
        self.assets.setUniformRowHeights(True)
        self.assets.setFrameShape(QFrame.Shape.NoFrame)
        self.assets.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assets.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.assets.setItemDelegate(AssetDelegate(self.assets))
        self.assets.setToolTip("Select assets, then add them to the map or download them")
        layout.addWidget(self.assets)
        self.no_assets = QLabel()
        self.no_assets.setWordWrap(True)
        layout.addWidget(self.no_assets)

        self.tile_styles = QLabel()
        self.tile_styles.setWordWrap(True)
        layout.addWidget(self.tile_styles)

        self.parquet_extent = QCheckBox("Read GeoParquet in the map extent only")
        self.parquet_extent.setChecked(True)
        self.parquet_extent.setToolTip(
            "DuckDB copies only the features that intersect the current map extent. "
            "Clear the box to copy the whole file."
        )
        layout.addWidget(self.parquet_extent)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _action_bar(self) -> QWidget:
        bar = QFrame()
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.add = QPushButton("Add to map")
        self.add.setIcon(QgsApplication.getThemeIcon("/mActionAddLayer.svg"))
        self.add.setDefault(True)
        self._accent(self.add)
        self.zoom = QPushButton("Zoom to")
        self.zoom.setIcon(QgsApplication.getThemeIcon("/mActionZoomToLayer.svg"))
        self.zoom.setToolTip("Zoom the map to the extent of this catalog, collection, or item")
        self.download = QToolButton()
        self.download.setText("Download")
        self.download.setIcon(QgsApplication.getThemeIcon("/mActionFileSave.svg"))
        self.download.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.download.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.download)
        self.download_selected = menu.addAction("Selected assets…")
        self.download_all = menu.addAction("Everything here…")
        self.download.setMenu(menu)
        buttons.addWidget(self.add, 1)
        buttons.addWidget(self.zoom)
        buttons.addWidget(self.download)
        layout.addLayout(buttons)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.hide()
        self.cancel = QPushButton("Cancel")
        self.cancel.hide()
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel)
        layout.addLayout(progress_row)
        return bar

    @staticmethod
    def _accent(button: QPushButton) -> None:
        """Paint the primary action in the Portolan blue."""
        hover = mix(ACCENT, QColor("#16170f"), 0.18).name()
        accent = ACCENT.name()
        button.setStyleSheet(
            f"QPushButton {{ background: {accent}; color: #fcfcfa; border: 1px solid {hover};"
            " border-radius: 3px; padding: 4px 12px; font-weight: bold; }"
            f"QPushButton:hover {{ background: {hover}; }}"
            "QPushButton:disabled { background: palette(midlight); color: palette(mid);"
            " border-color: palette(mid); font-weight: normal; }"
        )

    def _connect(self) -> None:
        self.search.textChanged.connect(self._show_catalogs)
        self.status.currentIndexChanged.connect(self._show_catalogs)
        self.in_extent.toggled.connect(self._show_catalogs)
        self.refresh.clicked.connect(self.reload)
        self._iface.mapCanvas().extentsChanged.connect(self._extent_changed)
        self.catalogs.itemClicked.connect(self._catalog_chosen)
        self.catalogs.itemActivated.connect(self._catalog_chosen)
        self.tree.itemClicked.connect(self._tree_clicked)
        self.tree.itemActivated.connect(self._tree_clicked)
        self.back.clicked.connect(lambda: self.pages.setCurrentIndex(_REGISTRY_PAGE))
        self.header.clicked.connect(self._show_catalog_details)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.currentItemChanged.connect(self._node_chosen)
        self.assets.itemSelectionChanged.connect(self._sync_buttons)
        self.assets.itemDoubleClicked.connect(lambda *_: self._add_selected())
        self.add.clicked.connect(self._add_selected)
        self.zoom.clicked.connect(self._zoom)
        self.download_selected.triggered.connect(self._download_selected)
        self.download_all.triggered.connect(self._download_all)
        self.cancel.clicked.connect(self._cancel_download)

    def disconnect_canvas(self) -> None:
        """Stop following the map canvas. Call before the dock is deleted."""
        with contextlib.suppress(TypeError):
            self._iface.mapCanvas().extentsChanged.disconnect(self._extent_changed)

    # ---------- registry ----------

    def reload(self) -> None:
        """Read the registry and list its catalogs."""
        self.catalogs.clear()
        self.shown.clear()
        self._placeholder("Reading the Portolan registry…")
        url = self._registry_url

        def read(_task: object) -> list[CatalogEntry]:
            return registry.parse_registry(fetch_json(url), url)

        run_task("Read the Portolan registry", read, self._registry_read)

    def _registry_read(self, result: object, error: BaseException | None) -> None:
        if error is not None or not isinstance(result, list):
            self.catalogs.clear()
            self._placeholder(
                "Could not read the Portolan registry. Check the connection, then reload."
            )
            self._warn(f"Could not read the Portolan registry: {error}")
            return
        self._entries = result
        current = self.status.currentData()
        self.status.blockSignals(True)
        self.status.clear()
        self.status.addItem("All statuses", None)
        for status in registry.statuses(result):
            self.status.addItem(status.capitalize(), status)
        index = self.status.findData(current)
        self.status.setCurrentIndex(max(index, 0))
        self.status.blockSignals(False)
        self._show_catalogs()

    def _extent_changed(self) -> None:
        if self.in_extent.isChecked():
            self._show_catalogs()

    def canvas_bbox(self) -> tuple[float, float, float, float] | None:
        """Return the map extent in WGS84, or None when it cannot be transformed."""
        canvas = self._iface.mapCanvas()
        transform = QgsCoordinateTransform(
            canvas.mapSettings().destinationCrs(),
            QgsCoordinateReferenceSystem(_WGS84),
            QgsProject.instance(),
        )
        try:
            rect = transform.transformBoundingBox(canvas.extent())
        except Exception:  # noqa: BLE001 - QgsCsException is not importable by name in every QGIS
            return None
        return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())

    def _show_catalogs(self) -> None:
        bbox = self.canvas_bbox() if self.in_extent.isChecked() else None
        shown = registry.filter_entries(
            self._entries, self.search.text(), self.status.currentData(), bbox
        )
        self.catalogs.clear()
        self._list_catalogs(shown)
        total = len(self._entries)
        if len(shown) == total:
            self.shown.setText(count(total, "catalog"))
        else:
            self.shown.setText(f"{len(shown):,} of {count(total, 'catalog')}")
        self.shown.setStyleSheet(f"color: {muted(self.palette()).name()};")
        if not shown and self._entries:
            self._placeholder("No catalog matches these filters.")

    def _list_catalogs(self, entries: list[CatalogEntry]) -> None:
        """Add one page of ``entries``, and a row that lists the rest."""
        for entry in entries[:CATALOG_PAGE]:
            item = QListWidgetItem(entry.title)
            item.setData(_ROLE, entry)
            tip = [html.escape(entry.title), html.escape(entry.url)]
            if entry.failure_reason:
                tip.append(html.escape(entry.failure_reason))
            item.setToolTip("<br>".join(tip))
            self.catalogs.addItem(item)
        rest = entries[CATALOG_PAGE:]
        if rest:
            more = More(tuple(rest))
            item = QListWidgetItem(more.label(CATALOG_PAGE))
            item.setData(_ROLE, more)
            self.catalogs.addItem(item)

    def _placeholder(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.catalogs.addItem(item)

    # ---------- catalog tree ----------

    def _catalog_chosen(self, item: QListWidgetItem | None) -> None:
        entry = item.data(_ROLE) if item is not None else None
        if isinstance(entry, More):
            self.catalogs.takeItem(self.catalogs.row(item))
            self._list_catalogs(list(entry.rest))
            return
        if not isinstance(entry, registry.CatalogEntry):
            return
        self.pages.setCurrentIndex(_CATALOG_PAGE)
        if entry is self._catalog:
            return
        self._catalog = entry
        self.header.show_entry(entry)
        if entry.logo_url:
            self.images.request(entry.logo_url, self._catalog_logo(entry))
        self.tree.clear()
        self._show_catalog_details()

        def opened(document: object, error: BaseException | None) -> None:
            if self._catalog is not entry:
                return
            if not isinstance(document, Document):
                self._show_failure(entry.title, entry.url, error)
                return
            if not entry.logo_url and document.icon:
                self.images.request(document.icon, self._catalog_logo(entry))
            self._fill(None, document)

        self._open(entry.url, opened)

    def _catalog_logo(self, entry: CatalogEntry) -> Callable[[Picture | None], None]:
        def show(picture: Picture | None) -> None:
            if self._catalog is entry:
                self.header.set_logo(picture)

        return show

    def _show_catalog_details(self) -> None:
        entry = self._catalog
        if entry is None:
            return
        self.tree.setCurrentItem(None)
        self._choose(entry.url, entry.title)

    def _node_item(self, node: Node) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.title])
        item.setData(0, _ROLE, node)
        item.setToolTip(0, node.href)
        item.setIcon(0, _kind_icon(node.kind))
        if node.kind != "item":
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        return item

    def _fill(self, parent: QTreeWidgetItem | None, document: Document) -> None:
        """List ``document``'s children under ``parent``, or at the top of the tree."""
        self._add_nodes(parent, document.children)

    def _add_nodes(self, parent: QTreeWidgetItem | None, nodes: tuple[Node, ...]) -> None:
        """Add one page of ``nodes``, and a row that lists the rest."""
        items = [self._node_item(node) for node in nodes[:NODE_PAGE]]
        rest = nodes[NODE_PAGE:]
        if rest:
            more = More(rest)
            row = QTreeWidgetItem([more.label(NODE_PAGE)])
            row.setData(0, _ROLE, more)
            row.setForeground(0, QBrush(ACCENT))
            row.setToolTip(0, f"This level lists {len(rest):,} more")
            items.append(row)
        if parent is None:
            self.tree.addTopLevelItems(items)
        else:
            parent.addChildren(items)
        for item in items:
            node = item.data(0, _ROLE)
            if isinstance(node, Node) and node.kind != "item":
                self._open(node.href, self._decorate(item), quiet=True)

    def _tree_clicked(self, item: QTreeWidgetItem | None, _column: int = 0) -> None:
        if item is None or not isinstance(more := item.data(0, _ROLE), More):
            return
        parent = item.parent()
        if parent is None:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        else:
            parent.removeChild(item)
        self._add_nodes(parent, more.rest)

    def _decorate(self, item: QTreeWidgetItem) -> Callable[[object, BaseException | None], None]:
        """Return a callback that shows a read child's icon, kind, and summary."""

        def opened(document: object, _error: BaseException | None) -> None:
            if not isinstance(document, Document):
                return
            with contextlib.suppress(RuntimeError):
                item.setIcon(0, _kind_icon(document.kind))
                if document.description:
                    item.setToolTip(0, _excerpt(document.description))
                if not document.children:
                    item.setChildIndicatorPolicy(
                        QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
                    )
            if document.icon:
                self.images.request(document.icon, self._tree_icon(item))

        return opened

    def _tree_icon(self, item: QTreeWidgetItem) -> Callable[[Picture | None], None]:
        def show(picture: Picture | None) -> None:
            if picture is None:
                return
            text = self.tree.palette().color(QPalette.ColorRole.Text)
            icon = QIcon(pixmap(picture, _ICON_SIZE, text, self.tree.devicePixelRatioF()))
            with contextlib.suppress(RuntimeError):
                item.setIcon(0, icon)

        return show

    def _open(
        self,
        href: str,
        then: Callable[[object, BaseException | None], None],
        quiet: bool = False,
    ) -> None:
        cached = self._documents.get(href)
        if cached is not None:
            then(cached, None)
            return
        waiting = self._reading.setdefault(href, [])
        waiting.append(then)
        if len(waiting) > 1:
            return

        def read(_task: object) -> Document:
            return read_document(fetch_json(href), href)

        def done(result: object, error: BaseException | None) -> None:
            if isinstance(result, Document):
                self._documents[href] = result
            for callback in self._reading.pop(href, []):
                callback(result, error)

        run_task(f"Read {href}", read, done, hidden=quiet)

    def _expand(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, _ROLE)
        if not isinstance(node, Node) or item.data(1, _ROLE):
            return
        item.setData(1, _ROLE, True)
        loading = QTreeWidgetItem(["Reading…"])
        loading.setFlags(Qt.ItemFlag.NoItemFlags)
        item.addChild(loading)

        def opened(document: object, error: BaseException | None) -> None:
            item.takeChildren()
            if not isinstance(document, Document):
                item.setData(1, _ROLE, False)
                failed = QTreeWidgetItem([f"Could not read: {error}"])
                failed.setFlags(Qt.ItemFlag.NoItemFlags)
                item.addChild(failed)
                return
            self._fill(item, document)
            if not document.children:
                item.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
                )

        self._open(node.href, opened)

    def _node_chosen(self, item: QTreeWidgetItem | None, _previous: object = None) -> None:
        node = item.data(0, _ROLE) if item is not None else None
        if isinstance(node, Node):
            self._choose(node.href, node.title, item)

    def _choose(self, href: str, title: str, item: QTreeWidgetItem | None = None) -> None:
        """Show the document at ``href`` once it is read."""
        self._document = None
        self.title.setText(title)
        self.description.setText("Reading…")
        self.facts.clear()
        self.assets.clear()
        self._fit_assets()
        self.hero.show_extent(None)
        self.hero.hide()
        self._sync_buttons()

        def opened(document: object, error: BaseException | None) -> None:
            if self.tree.currentItem() is not item:
                return
            if not isinstance(document, Document):
                self._show_failure(title, href, error)
                return
            self._show_document(document)

        self._open(href, opened)

    def _show_failure(self, title: str, href: str, error: BaseException | None) -> None:
        self.title.setText(title)
        self.description.setTextFormat(Qt.TextFormat.PlainText)
        self.description.setText(f"Could not read {href}: {error}")
        self.description.setTextFormat(Qt.TextFormat.MarkdownText)

    # ---------- details and assets ----------

    def _extent(self, document: Document) -> Bbox | None:
        """Return the document's extent.

        A STAC catalog declares none, so the registry's extent for the open
        catalog stands in for it.
        """
        if document.bbox is not None:
            return document.bbox
        entry = self._catalog
        return entry.bbox if entry is not None and entry.url == document.href else None

    def _show_document(self, document: Document) -> None:
        self._document = document
        bbox = self._extent(document)
        self.hero.show_extent(bbox)
        self.hero.setVisible(bbox is not None)
        thumb = document.thumbnail
        if thumb is not None:

            def show(picture: Picture | None) -> None:
                if self._document is document and picture is not None:
                    self.hero.set_picture(picture)
                    self.hero.show()

            self.images.request(thumb.href, show)
        self.title.setText(document.title)
        self.description.setText(document.description)
        self.description.setVisible(bool(document.description))
        self.facts.setText(_facts_html(document, bbox, muted(self.palette())))
        self.download_all.setText(f"Everything in {_short(document.title)}…")

        self.assets.clear()
        # The advice names the PMTiles, so it shows only when the collection has them.
        advice = VIEW_OR_ANALYZE if layer_io.archive_urls(document) else ""
        for link in document.pmtiles:
            row = QTreeWidgetItem([link.title or "Vector tiles"])
            row.setData(0, _ROLE, _PMTILES_KEY + link.href)
            row.setData(0, BADGE_ROLE, ("PMTiles", "pmtiles", ""))
            row.setToolTip(0, _asset_tip(link.href, "Layers", ", ".join(link.layers), advice))
            self.assets.addTopLevelItem(row)
        # Assets QGIS can open come first, then styles, thumbnails, and sidecars.
        for asset in sorted(document.assets, key=lambda a: a.format is None):
            row = QTreeWidgetItem([asset.label])
            row.setData(0, _ROLE, asset)
            badge = format_label(asset.format, asset.type, asset.href)
            row.setData(0, BADGE_ROLE, (badge, asset.format, human_size(asset.size)))
            note = advice if asset.format in {"pmtiles", "parquet"} else ""
            row.setToolTip(0, _asset_tip(asset.href, "Roles", ", ".join(asset.roles), note))
            self.assets.addTopLevelItem(row)
        self._fit_assets()
        self._select_preferred(document)

        styles = len(document.styles)
        self.tile_styles.setText(
            f"The collection has {count(styles, 'style')} for its vector tiles. "
            "After you add the tiles, switch styles from the layer's Styles menu."
        )
        self.tile_styles.setStyleSheet(f"color: {muted(self.palette()).name()};")
        self.tile_styles.setVisible(bool(styles and layer_io.archive_urls(document)))
        self.parquet_extent.setVisible(any(a.format == "parquet" for a in document.assets))
        self.add.setToolTip(advice)
        self._sync_buttons()

    def _select_preferred(self, document: Document) -> None:
        """Select the asset that Add to map should add when the user picks nothing else."""
        href = document.preferred_href(parquet=parquet_query.duckdb_status()[0])
        if href is None:
            return
        for i in range(self.assets.topLevelItemCount()):
            row = self.assets.topLevelItem(i)
            value = row.data(0, _ROLE)
            if value == _PMTILES_KEY + href or (isinstance(value, Asset) and value.href == href):
                row.setSelected(True)
                return

    def _fit_assets(self) -> None:
        """Size the asset list to its rows, so the details panel scrolls instead."""
        rows = self.assets.topLevelItemCount()
        self.assets.setVisible(rows > 0)
        self.assets_heading.setVisible(rows > 0)
        self.assets_heading.setText(f"Assets ({rows})")
        if rows:
            height = sum(self.assets.sizeHintForRow(i) for i in range(rows))
            self.assets.setFixedHeight(height + 2 * self.assets.frameWidth() + 2)
        document = self._document
        if document is not None and not rows:
            self.no_assets.setText(
                "This catalog has no files of its own. Open a collection in the tree above."
                if document.children
                else "This entry lists no assets."
            )
            self.no_assets.setStyleSheet(f"color: {muted(self.palette()).name()};")
            self.no_assets.show()
        else:
            self.no_assets.hide()
        if document is None:
            self.tile_styles.hide()
            self.parquet_extent.hide()

    def _selected(self) -> tuple[list[str], list[Asset]]:
        tiles: list[str] = []
        assets: list[Asset] = []
        for row in self.assets.selectedItems():
            value = row.data(0, _ROLE)
            if isinstance(value, str) and value.startswith(_PMTILES_KEY):
                tiles.append(value[len(_PMTILES_KEY) :])
            elif isinstance(value, Asset):
                if value.format == "pmtiles":
                    tiles.append(value.href)
                else:
                    assets.append(value)
        return tiles, assets

    def _sync_buttons(self) -> None:
        tiles, assets = self._selected()
        loadable = [a for a in assets if a.format is not None]
        self.add.setEnabled(bool(tiles or loadable))
        has_document = self._document is not None
        self.zoom.setEnabled(
            self._document is not None and self._extent(self._document) is not None
        )
        self.download_selected.setEnabled(bool(tiles or assets) and self._job is None)
        self.download_all.setEnabled(has_document and self._job is None)
        self.download.setEnabled(
            self.download_selected.isEnabled() or self.download_all.isEnabled()
        )

    def _add_selected(self) -> None:
        document = self._document
        if document is None:
            return
        tiles, assets = self._selected()
        if tiles:
            self._add_tiles(document, tiles)
        for asset in assets:
            if asset.format == "parquet":
                self._add_parquet(asset)
            elif asset.format is not None:
                self._add_asset(asset)

    def _add_tiles(self, document: Document, urls: list[str]) -> None:
        picked = list(dict.fromkeys(urls))

        def prepare(_task: object) -> layer_io.PreparedTiles:
            return layer_io.prepare_pmtiles(document, picked, fetch_bytes)

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, layer_io.PreparedTiles):
                self._warn(f"Could not add vector tiles: {error}")
                return
            try:
                layers, warnings = layer_io.build_pmtiles(
                    self._server, result, lambda data: QImage.fromData(data)
                )
            except layer_io.LayerError as failure:
                self._warn(str(failure))
                return
            for note in warnings:
                _log(note, Qgis.MessageLevel.Warning)
            for layer in layers:
                QgsProject.instance().addMapLayer(layer)
            self._info(f"Added {len(layers)} vector tile layer(s) from {document.title}.")

        run_task(f"Open vector tiles of {document.title}", prepare, done)

    def _add_asset(self, asset: Asset) -> None:
        try:
            layer = layer_io.asset_layer(asset)
        except layer_io.LayerError as failure:
            self._warn(str(failure))
            return
        QgsProject.instance().addMapLayer(layer)
        self._info(f"Added {asset.label}.")

    def _add_parquet(self, asset: Asset) -> None:
        ok, version = parquet_query.duckdb_status()
        if not ok:
            self._duckdb_help(version)
            return
        canvas = self._iface.mapCanvas()
        extent = canvas.extent() if self.parquet_extent.isChecked() else None
        extent_crs = canvas.mapSettings().destinationCrs()
        context = QgsProject.instance().transformContext()
        extension_dir = parquet_layer.extension_directory()
        name = asset.title or asset.href.rsplit("/", 1)[-1].removesuffix(".parquet")

        in_extent = extent is not None
        document = self._document
        has_tiles = document is not None and bool(layer_io.archive_urls(document))

        def read(task: Any) -> parquet_layer.Planned:
            return parquet_layer.plan(
                asset.href, context, extent, extent_crs, extension_dir, task.isCanceled
            )

        def planned(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_layer.Planned):
                if type(error).__name__ == "InterruptException":
                    self._info(f"Stopped reading {name}.")
                else:
                    self._warn(f"DuckDB could not read {asset.href}: {error}")
                return
            estimate = result.estimate or 0
            if estimate > CONFIRM_FEATURES and not self._confirm_features(
                name, estimate, in_extent, has_tiles
            ):
                self._info(f"Did not load {name}.")
                return
            self._copy_parquet(result, name, extension_dir, in_extent)

        self._info(f"Reading {name} with DuckDB…")
        run_task(f"Read {asset.href} with DuckDB", read, planned)

    def _confirm_features(self, name: str, estimate: int, in_extent: bool, has_tiles: bool) -> bool:
        where = "in the map extent" if in_extent else "in the file"
        advice = f"{VIEW_OR_ANALYZE}\n\n" if has_tiles else ""
        text = (
            f"{name} holds about {estimate:,} features {where}. DuckDB copies them to a "
            "GeoPackage on disk, which takes time and disk space.\n\n"
            f"{advice}Load the features?"
        )
        answer = QMessageBox.question(self, "Load GeoParquet", text)
        return answer == QMessageBox.StandardButton.Yes

    def _copy_parquet(
        self, planned: parquet_layer.Planned, name: str, extension_dir: str, in_extent: bool
    ) -> None:
        def copy(task: Any) -> parquet_layer.Prepared:
            return parquet_layer.prepare(planned, extension_dir, cancelled=task.isCanceled)

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_layer.Prepared):
                if type(error).__name__ == "InterruptException":
                    self._info(f"Stopped the copy of {name}.")
                else:
                    self._warn(f"DuckDB could not read {planned.url}: {error}")
                return
            if not result.written.features:
                parquet_layer.discard_path(result.path)
                self._info(
                    "No feature intersects the map extent."
                    if in_extent
                    else f"{name} holds no features."
                )
                return
            try:
                layer, refused = parquet_layer.build(result, name)
            except OSError as failure:
                self._warn(str(failure))
                return
            QgsProject.instance().addMapLayer(layer)
            notes = [f"Added {result.written.features:,} features from {name}."]
            if refused:
                notes.append(
                    f"{refused:,} of them have no geometry, because their geometry type "
                    f"does not fit a {result.written.geometry_type} layer."
                )
            (self._warn if refused else self._info)(" ".join(notes))

        about = f"about {planned.estimate:,}" if planned.estimate is not None else "the"
        self._info(f"Copying {about} features of {name} with DuckDB…")
        run_task(f"Copy {planned.url} with DuckDB", copy, done)

    def _duckdb_help(self, version: str | None) -> None:
        minimum = ".".join(str(part) for part in parquet_query.MINIMUM_DUCKDB)
        if version:
            heading = (
                f"DuckDB {html.escape(version)} is too old. GeoParquet needs {minimum} or newer."
            )
        else:
            heading = f"GeoParquet needs DuckDB {minimum} or newer, and it is not installed."
        command = html.escape(parquet_query.install_command(upgrade=version is not None))
        where = (
            "QGIS runs in a Flatpak, so install DuckDB into your user folder from a terminal:"
            if parquet_query.is_flatpak()
            else "Install it from a terminal with the Python that QGIS uses:"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Portolan Registry: DuckDB needed")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<p><b>{heading}</b></p><p>{where}</p><pre>{command}</pre><p>Then restart QGIS.</p>"
        )
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.exec()

    def _zoom(self) -> None:
        document = self._document
        bbox = self._extent(document) if document is not None else None
        if bbox is None:
            return
        canvas = self._iface.mapCanvas()
        transform = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem(_WGS84),
            canvas.mapSettings().destinationCrs(),
            QgsProject.instance(),
        )
        try:
            extent = transform.transformBoundingBox(QgsRectangle(*bbox))
        except Exception as error:  # noqa: BLE001 - see canvas_bbox
            self._warn(f"Could not transform the extent: {error}")
            return
        canvas.setExtent(extent)
        canvas.refresh()

    # ---------- downloads ----------

    def _choose_folder(self) -> Path | None:
        folder = QFileDialog.getExistingDirectory(self, "Download into folder")
        return Path(folder) if folder else None

    def _root_url(self) -> str | None:
        return self._catalog.url if self._catalog is not None else None

    def _download_selected(self) -> None:
        document = self._document
        if document is None:
            return
        tiles, assets = self._selected()
        root = self._root_url() or document.href
        files = [
            download.PlannedFile(a.href, download.local_path(a.href, root), a.size, a.checksum)
            for a in assets
        ]
        files += [download.PlannedFile(url, download.local_path(url, root)) for url in tiles]
        folder = self._choose_folder()
        if folder is not None:
            self._start_download(folder, files)

    def _download_all(self) -> None:
        document = self._document
        if document is None:
            return
        folder = self._choose_folder()
        if folder is None:
            return
        root = self._root_url() or document.href
        self._show_progress(0, 0, f"Listing everything below {document.title}…")

        def build(task: Any) -> download.Plan:
            executor = QtExecutor(download.WALK_WORKERS)
            try:
                return download.plan(
                    fetch_json, document.href, root, cancelled=task.isCanceled, executor=executor
                )
            finally:
                executor.shutdown(cancel_futures=True)

        def planned(result: object, error: BaseException | None) -> None:
            self._hide_progress()
            if not isinstance(result, download.Plan):
                self._warn(f"Could not list {document.title}: {error}")
                return
            if self._confirm(result):
                self._start_download(folder, result.files)

        run_task(f"List {document.title}", build, planned)

    def _confirm(self, plan: download.Plan) -> bool:
        lines = [f"{len(plan.files)} files, at least {human_size(plan.known_bytes)}."]
        if plan.unknown_sizes:
            lines.append(f"{plan.unknown_sizes} files do not declare a size.")
        if plan.failures:
            lines.append(f"{len(plan.failures)} documents could not be read and are left out.")
        if plan.truncated:
            lines.append("The listing stopped at its document limit.")
        answer = QMessageBox.question(self, "Download", "\n".join([*lines, "", "Download now?"]))
        return answer == QMessageBox.StandardButton.Yes

    def _start_download(self, folder: Path, files: list[download.PlannedFile]) -> None:
        job = DownloadJob(folder, files, self)
        job.progress.connect(self._show_progress)
        job.finished.connect(lambda report: self._downloaded(folder, report))
        self._job = job
        self.cancel.show()
        self._sync_buttons()
        job.start()

    def _cancel_download(self) -> None:
        if self._job is not None:
            self._job.cancel()

    def _downloaded(self, folder: Path, report: DownloadReport) -> None:
        self._job = None
        self._hide_progress()
        self._sync_buttons()
        for path, reason in report.failed:
            _log(f"{path}: {reason}", Qgis.MessageLevel.Warning)
        summary = (
            f"{len(report.downloaded)} downloaded, {len(report.skipped)} already present, "
            f"{report.verified} checksums verified, {len(report.failed)} failed"
        )
        if report.cancelled:
            self._warn(f"Download cancelled: {summary}.")
        elif report.failed:
            self._warn(f"Download into {folder} finished with errors: {summary}.")
        else:
            self._info(f"Download into {folder} finished: {summary}.")

    def _show_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress.setFormat(f"{message}  (%v/%m)" if total else message)
        self.progress.show()

    def _hide_progress(self) -> None:
        self.progress.hide()
        self.cancel.hide()

    # ---------- messages ----------

    def _info(self, text: str) -> None:
        self._iface.messageBar().pushMessage(LOG_TAG, text, Qgis.MessageLevel.Info, 5)

    def _warn(self, text: str) -> None:
        _log(text, Qgis.MessageLevel.Warning)
        self._iface.messageBar().pushMessage(LOG_TAG, text, Qgis.MessageLevel.Warning, 10)


def _excerpt(text: str, limit: int = 240) -> str:
    """Return the first ``limit`` characters of ``text`` on one line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _short(title: str, limit: int = 28) -> str:
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def _asset_tip(href: str, label: str, value: str, note: str = "") -> str:
    lines = [html.escape(href)]
    if value:
        lines.append(f"{label}: {html.escape(value)}")
    if note:
        lines.append(html.escape(note))
    return "<br>".join(lines)


def _facts_html(document: Document, bbox: Bbox | None, label_color: QColor) -> str:
    """Return the document's facts as a two-column table, then its links."""
    facts = [("Type", document.kind.capitalize()), ("ID", document.id)]
    if document.license:
        facts.append(("License", document.license))
    if bbox is not None:
        west, south, east, north = bbox
        facts.append(("Extent", f"{west:.4f}, {south:.4f} to {east:.4f}, {north:.4f}"))
    color = label_color.name()
    rows = "".join(
        f'<tr><td style="color: {color}; padding-right: 10px;">{html.escape(key)}</td>'
        f"<td>{html.escape(value)}</td></tr>"
        for key, value in facts
    )
    links = [
        f'<a href="{html.escape(link.href)}">{html.escape(link.title or link.rel.capitalize())}</a>'
        for link in document.links
        if link.rel in {"describedby", "license", "via"}
    ]
    links.append(f'<a href="{html.escape(document.href)}">STAC document</a>')
    return f"<table>{rows}</table><p>{'&nbsp;&nbsp;&nbsp;'.join(links)}</p>"
