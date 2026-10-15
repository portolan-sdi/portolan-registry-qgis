"""The Portolan Registry window: the publisher list and tree, and the details pane.

Background results compare the open document by identity before they show.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QByteArray, QEvent, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QPalette
from qgis.PyQt.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from portolan_registry_qgis.core import registry
from portolan_registry_qgis.core.facts import document_extent
from portolan_registry_qgis.core.hrefs import BROWSER_URL, browser_url
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.loadable import only_items
from portolan_registry_qgis.core.scope import Area, Everything, MapView, Scope
from portolan_registry_qgis.core.stac import Asset, Document
from portolan_registry_qgis.core.style import archive_urls
from portolan_registry_qgis.gui.catalog_tree import CatalogTree
from portolan_registry_qgis.gui.details import SETTINGS_GROUP, DetailsPanel
from portolan_registry_qgis.gui.downloads import Downloads
from portolan_registry_qgis.gui.loading import Loader, duckdb_ready
from portolan_registry_qgis.gui.pictures import (
    ImageCache,
    WorldMap,
    decode,
    logomark,
    muted,
    pixmap,
)
from portolan_registry_qgis.gui.registry_list import RegistryList
from portolan_registry_qgis.gui.scope import ScopeError
from portolan_registry_qgis.gui.tasks import TaskController
from portolan_registry_qgis.gui.widgets import open_url, theme_icon
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.canvas import CanvasError, zoom_to
from portolan_registry_qgis.qgis_io.documents import DocumentCache
from portolan_registry_qgis.qgis_io.log import Messages

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from qgis.PyQt.QtWidgets import QTreeWidgetItem

    from portolan_registry_qgis.core.registry import CatalogEntry
    from portolan_registry_qgis.core.stac import Bbox
    from portolan_registry_qgis.gui.pictures import Picture
    from portolan_registry_qgis.qgis_io.tileserver import TileServer

_LOGOMARK_SIZE = QSize(20, 20)
_REGISTRY_PAGE, _CATALOG_PAGE = 0, 1
# The window keeps its size, its splitter, and the open state of each
# section in these QGIS settings, from one session to the next.
GEOMETRY_SETTING = f"{SETTINGS_GROUP}/geometry"
SPLITTER_SETTING = f"{SETTINGS_GROUP}/splitter"
DEFAULT_SIZE = QSize(1000, 700)


class RegistryWindow(QDialog):
    """Browse the Portolan registry, add its data to the map, and download it.

    The window is not modal, so the user can pan the map while it is open.
    """

    # Emits True when the window shows and False when it hides.
    visibility_changed = pyqtSignal(bool)

    def __init__(
        self,
        iface: Any,
        server: TileServer,
        registry_url: str = registry.REGISTRY_URL,
        browser_base: str = BROWSER_URL,
    ):
        super().__init__(iface.mainWindow())
        self.setWindowTitle("Portolan Registry")
        self.setObjectName("PortolanRegistryWindow")
        self.setModal(False)
        self.resize(DEFAULT_SIZE)
        self._iface = iface
        self._registry_url = registry_url
        self._browser_base = browser_base
        self._catalog: CatalogEntry | None = None
        self._document: Document | None = None
        self.images = ImageCache(self)
        self.world = WorldMap(self)
        self.logomark_file: Path | None = None
        self.messages = Messages(iface)
        self._documents = DocumentCache()
        self.tasks = TaskController(self.messages, self)
        self.registry_list = RegistryList(
            iface, self.images, self.world, self.messages, self.browser_link
        )
        self.catalog_tree = CatalogTree(self.world, self.images, self._documents)
        self.details = DetailsPanel(
            iface, self.images, self.world, self._documents, lambda: self._document
        )
        self._loader = Loader(self, iface, server, self.messages, self.tasks)
        self._downloads = Downloads(self, self.messages, self.tasks)
        self._build()
        self._restore_layout()
        self.reload()

    @property
    def document(self) -> Document | None:
        """Return the document that the details panel shows, or None."""
        return self._document

    def is_busy(self) -> bool:
        """Return whether a mosaic search, a download listing, or a download runs."""
        return self.tasks.is_busy()

    # ---------- layout ----------

    def _build(self) -> None:
        self.pages = QStackedWidget()
        self.pages.addWidget(self.registry_list)
        self.pages.addWidget(self.catalog_tree)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.pages)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([400, 600])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._header())
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.tasks)
        # In a dialog, a push button with focus becomes the default, and an
        # Enter that the tree or the search box passes on would click it.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self._connect()
        self._sync_buttons()

    def _header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(8)
        self.logo = QLabel()
        self.logo.setFixedSize(_LOGOMARK_SIZE)
        layout.addWidget(self.logo)
        name = QLabel("Portolan")  # i18n: skip
        font = name.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        name.setFont(font)
        layout.addWidget(name)
        self.tagline = QLabel(_("Open data registry"))
        layout.addWidget(self.tagline)
        layout.addStretch(1)
        self.refresh = QToolButton()
        self.refresh.setIcon(theme_icon("mActionRefresh"))
        self.refresh.setAutoRaise(True)
        self.refresh.setToolTip(_("Read the registry again"))
        layout.addWidget(self.refresh)
        self._show_logomark()
        return header

    def _show_logomark(self) -> None:
        """Show the logomark that suits the theme, and mute the tagline."""
        palette = self.palette()
        path = logomark(palette)
        self.logomark_file = path
        picture = decode(path.read_bytes())
        if picture is not None:
            text = palette.color(QPalette.ColorRole.Text)
            self.logo.setPixmap(pixmap(picture, _LOGOMARK_SIZE, text, self.devicePixelRatioF()))
        self.tagline.setStyleSheet(f"color: {muted(palette).name()};")

    def changeEvent(self, event: Any) -> None:
        """Show the logomark again when the theme changes."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "tagline"):
            self._show_logomark()

    def _restore_layout(self) -> None:
        """Restore the size and splitter that the window had when it last hid."""
        settings = QgsSettings()
        geometry = settings.value(GEOMETRY_SETTING)
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
        splitter = settings.value(SPLITTER_SETTING)
        if isinstance(splitter, QByteArray) and not splitter.isEmpty():
            self.splitter.restoreState(splitter)

    def _save_layout(self) -> None:
        settings = QgsSettings()
        settings.setValue(GEOMETRY_SETTING, self.saveGeometry())
        settings.setValue(SPLITTER_SETTING, self.splitter.saveState())

    def _connect(self) -> None:
        self.refresh.clicked.connect(self.reload)
        self.registry_list.catalog_chosen.connect(self._catalog_chosen)
        self.catalog_tree.back.clicked.connect(lambda: self.pages.setCurrentIndex(_REGISTRY_PAGE))
        self.catalog_tree.header.clicked.connect(self._show_catalog_details)
        self.catalog_tree.node_chosen.connect(self._choose)
        self.tasks.busy_changed.connect(self._sync_buttons)
        details = self.details
        details.state_changed.connect(self._sync_buttons)
        details.assets.itemDoubleClicked.connect(lambda *_args: self._add_selected())
        details.add.clicked.connect(self._add_selected)
        details.zoom.clicked.connect(self._zoom)
        details.download_selected.triggered.connect(self._download_selected)
        details.download_all.triggered.connect(self.start_download)
        details.view_button.clicked.connect(self._view_on_map)
        details.load_button.clicked.connect(self._load_full_data)
        details.mosaic_button.clicked.connect(self.add_mosaic)
        details.open_browser.clicked.connect(self._open_in_browser)

    def showEvent(self, event: Any) -> None:
        """Report that the window shows."""
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event: Any) -> None:
        """Save the window's layout, and report that the window hides.

        The drawn area leaves the canvas, and the map tool that was active
        before the drawing comes back.
        """
        self._save_layout()
        self.details.scope_picker.clear()
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def disconnect_canvas(self) -> None:
        """Stop following the map canvas and the project. Call before the window is deleted."""
        self.registry_list.release()
        self.details.release()

    def reload(self) -> None:
        """Read the registry and list its catalogs."""
        self.registry_list.reload(self._registry_url)

    # ---------- the open publisher and document ----------

    def _catalog_chosen(self, entry: CatalogEntry) -> None:
        self.pages.setCurrentIndex(_CATALOG_PAGE)
        if entry is self._catalog:
            return
        self._catalog = entry
        self.catalog_tree.show_entry(entry)
        if entry.logo_url:
            self.images.request(entry.logo_url, self._catalog_logo(entry))
        self._show_catalog_details()

        def opened(document: object, error: BaseException | None) -> None:
            if self._catalog is not entry:
                return
            if not isinstance(document, Document):
                self.details.show_failure(entry.title, entry.url, error)
                return
            if not entry.logo_url and document.icon:
                self.images.request(document.icon, self._catalog_logo(entry))
            self.catalog_tree.fill(None, document)

        self._documents.open(entry.url, opened)

    def _catalog_logo(self, entry: CatalogEntry) -> Callable[[Picture | None], None]:
        def show(picture: Picture | None) -> None:
            if self._catalog is entry:
                self.catalog_tree.header.set_logo(picture)

        return show

    def _show_catalog_details(self) -> None:
        entry = self._catalog
        if entry is None:
            return
        self.catalog_tree.tree.setCurrentItem(None)
        self._choose(entry.url, entry.title)

    def _choose(self, href: str, title: str, item: QTreeWidgetItem | None = None) -> None:
        """Show the document at ``href`` once it is read."""
        self._document = None
        self.details.show_reading(title, self.browser_link(self._browser_target()))
        self._sync_buttons()
        tree = self.catalog_tree.tree

        def opened(document: object, error: BaseException | None) -> None:
            if tree.currentItem() is not item:
                return
            if not isinstance(document, Document):
                self.details.show_failure(title, href, error)
                return
            self.show_document(document)

        self._documents.open(href, opened)

    def show_document(self, document: Document) -> None:
        """Show ``document`` in the details panel, with its files and actions."""
        self._document = document
        self.details.show_document(document, self._catalog, self.browser_link(document.href))
        self._sync_buttons()

    def _extent(self) -> Bbox | None:
        document = self._document
        return document_extent(document, self._catalog) if document is not None else None

    def _sync_buttons(self) -> None:
        self.details.sync_buttons(
            self._document, zoomable=self._extent() is not None, busy=self.tasks.is_busy()
        )

    # ---------- actions ----------

    def _add_selected(self) -> None:
        document = self._document
        if document is None:
            return
        tiles, assets = self.details.selected()
        if tiles:
            self._loader.add_tiles(document, tiles)
        for asset in assets:
            self._add_file(asset, whole=True)

    def _add_file(self, asset: Asset, *, whole: bool = False) -> None:
        """Add one file. ``whole`` skips the area, as the All files buttons do."""
        if asset.format == "parquet":
            self._add_parquet(asset, whole=whole)
        else:
            self._loader.add_asset(asset)

    def _view_on_map(self) -> None:
        document, target = self._document, self.details.targets.view
        if document is None or target is None:
            return
        if target.asset is None or target.format == "pmtiles":
            self._loader.add_tiles(document, [target.href])
        else:
            self._add_file(target.asset)

    def _load_full_data(self) -> None:
        target = self.details.targets.load
        if target is not None and target.asset is not None:
            self._add_file(target.asset)

    def _parquet_scope(self) -> tuple[Scope, parquet_layer.Limits] | None:
        """Return the scope and the ``parquet_layer.Limits`` it gives.

        A map view passes the canvas extent in the canvas CRS, which is
        exact. An area passes its WKT in EPSG:4326. The method warns and
        returns None when the scope is not complete.
        """
        try:
            scope = self.details.scope()
        except ScopeError as error:
            self.messages.warn(str(error))
            return None
        canvas = self._iface.mapCanvas()
        if isinstance(scope, MapView):
            crs = canvas.mapSettings().destinationCrs()
            return scope, parquet_layer.Limits(extent=canvas.extent(), extent_crs=crs)
        if isinstance(scope, Area):
            return scope, parquet_layer.Limits(area=scope.wkt)
        return scope, parquet_layer.Limits()

    def _add_parquet(self, asset: Asset, *, whole: bool = False) -> None:
        if not duckdb_ready(self):
            return
        # A glob that HTTPS serves walks the items, which shares the mosaic's progress bar.
        if asset.read_url is None and self.tasks.refuse_when_busy():
            return
        scoped: tuple[Scope, parquet_layer.Limits] | None = (Everything(), parquet_layer.Limits())
        if not whole:
            scoped = self._parquet_scope()
        if scoped is None:
            return
        document = self._document
        has_tiles = document is not None and bool(archive_urls(document))
        self._loader.add_parquet(asset, document, *scoped, has_tiles=has_tiles)

    def _zoom(self) -> None:
        bbox = self._extent()
        if bbox is None:
            return
        try:
            zoom_to(self._iface.mapCanvas(), bbox)
        except CanvasError as error:
            self.messages.warn(_("Could not transform the extent: {error}").format(error=error))

    def add_mosaic(self) -> None:
        """Add the files of the open collection's items in the scope as one layer."""
        document = self._document
        if document is None or not only_items(document) or self.tasks.refuse_when_busy():
            return
        scoped = self._parquet_scope()
        if scoped is not None:
            self._loader.add_mosaic(document, *scoped)

    def _root_url(self, document: Document) -> str:
        """Return the URL that download paths start from."""
        entry = self._catalog
        return (entry.url if entry is not None else None) or document.href

    def _download_selected(self) -> None:
        """Download the selected files, whole, whatever the area."""
        document = self._document
        if document is None or self.tasks.refuse_when_busy():
            return
        tiles, assets = self.details.selected()
        self._downloads.download_files(self._root_url(document), tiles, assets)

    def start_download(self) -> None:
        """Download everything in the area.

        The walk leaves out the items outside the area. With a map view or
        an area, each GeoParquet file of the document downloads as a
        GeoPackage of the features in the area.
        """
        document = self._document
        if document is None or self.tasks.refuse_when_busy():
            return
        scoped = self._parquet_scope()
        if scoped is None:
            return
        self._downloads.download_all(document, self._root_url(document), *scoped)

    # ---------- browser ----------

    def browser_link(self, href: str | None) -> str | None:
        """Return the Portolan browser page of the document at ``href``.

        Returns None for a document that is not on http(s), because the
        browser cannot read it.
        """
        if not href:
            return None
        try:
            return browser_url(href, self._browser_base)
        except ValueError:
            return None

    def _browser_target(self) -> str | None:
        """Return the href of the open document, else of the open publisher."""
        if self._document is not None:
            return self._document.href
        return self._catalog.url if self._catalog is not None else None

    def _open_in_browser(self) -> None:
        link = self.browser_link(self._browser_target())
        if link is not None:
            open_url(link)
