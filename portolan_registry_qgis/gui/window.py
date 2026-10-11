"""The Portolan Registry window.

A header with the Portolan mark runs across the top. Below it, a splitter
holds two panes. The left pane has two pages. The first lists the registry's
publishers, each with a map of its extent and its logo. A click opens the
second page, which shows the publisher's tree. The right pane shows the
chosen node in collapsible sections: its picture and description, the ways to
add it to the map, its files, and its details. The action bar runs across the
bottom, with the actions that add the selected files to the map or download
them.

The "Add to map" section offers at most two primary buttons, "View on map"
and "Load full data", and the scope picker. The scope limits what a
GeoParquet add, a mosaic, and a download read. A collection whose children
are all items shows one "N files" row in the tree, and its section offers
"Add as one layer", which adds the COG files of those items as one mosaic.
"""

from __future__ import annotations

import contextlib
import html
import shutil
from dataclasses import dataclass
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
    QgsSettings,
)
from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import QByteArray, QEvent, QSize, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QDesktopServices, QIcon, QImage, QPalette
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
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
from portolan_registry_qgis.core.format import (
    count,
    format_label,
    human_size,
    license_name,
    link_label,
)
from portolan_registry_qgis.core.hrefs import BROWSER_URL, browser_url, vsicurl_path
from portolan_registry_qgis.core.scope import Area, Everything, MapView, Scope
from portolan_registry_qgis.core.stac import Asset, Document, Loadable, Node, read_document
from portolan_registry_qgis.gui.pictures import (
    ACCENT,
    ImageCache,
    WorldMap,
    decode,
    logomark,
    mix,
    muted,
    pixmap,
)
from portolan_registry_qgis.gui.scope import ScopeError, ScopePicker
from portolan_registry_qgis.gui.widgets import (
    BADGE_ROLE,
    ON_MAP_ROLE,
    ROLE,
    AssetDelegate,
    CatalogDelegate,
    CatalogHeader,
    HeroView,
    More,
    RoomyDelegate,
)
from portolan_registry_qgis.qgis_io import layers as layer_io
from portolan_registry_qgis.qgis_io import mosaic, parquet_layer
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
# The message after an add that moved the map to the new layers.
_ZOOMED = " The map moved to show it."
# The lists show this many rows, then a row that shows the next page. The
# tree reads the child documents of each page it shows, for their icons.
CATALOG_PAGE = 25
NODE_PAGE = 50
# A GeoParquet load above this many features asks first. A load of any size
# works, but it takes time and disk space.
CONFIRM_FEATURES = 1_000_000
# A mosaic of more than this many files asks first. GDAL opens each file to
# write the VRT.
CONFIRM_FILES = 500
# The two primary buttons. Each adds the first file of its formats, in order.
VIEW_TIP = "Fast. Draws with the publisher's style."
LOAD_TIP = "All features and attributes, for analysis."
TABLE_TIP = "All rows and columns, as a table with no geometry."
VIEW_FORMATS = ("pmtiles", "cog")
LOAD_FORMATS = ("parquet", "flatgeobuf", "geojson", "csv")
BROWSER_LINK = "All files and metadata in the Portolan browser"
_KIND_ICONS = {
    "catalog": "/mIconFolder.svg",
    "collection": "/mIconLayerTree.svg",
    "item": "/mIconFile.svg",
}
_ICON_SIZE = QSize(18, 18)
_LOGOMARK_SIZE = QSize(20, 20)
_REGISTRY_PAGE, _CATALOG_PAGE = 0, 1
# The window keeps its size, its splitter, and the open state of each
# section in these QGIS settings, from one session to the next.
SETTINGS_GROUP = "PortolanRegistry"
GEOMETRY_SETTING = f"{SETTINGS_GROUP}/geometry"
SPLITTER_SETTING = f"{SETTINGS_GROUP}/splitter"
DEFAULT_SIZE = QSize(1000, 700)


def _log(message: str, level: Any = None) -> None:
    QgsMessageLog.logMessage(message, LOG_TAG, level or Qgis.MessageLevel.Info)


def _kind_icon(kind: str) -> QIcon:
    return QgsApplication.getThemeIcon(_KIND_ICONS.get(kind, _KIND_ICONS["item"]))


@dataclass(frozen=True)
class ItemFiles:
    """The tree row that stands for every item of a collection."""

    href: str
    title: str
    count: int


def only_items(document: Document) -> bool:
    """Return whether every child of ``document`` is an item."""
    return bool(document.children) and all(node.kind == "item" for node in document.children)


def _first(loadable: tuple[Loadable, ...], formats: tuple[str, ...]) -> Loadable | None:
    """Return the first file of the first format in ``formats`` that has one."""
    for wanted in formats:
        for candidate in loadable:
            if candidate.format == wanted:
                return candidate
    return None


def _section(
    title: str, name: str, collapsed: bool = False
) -> tuple[QgsCollapsibleGroupBox, QVBoxLayout]:
    """Return a collapsible section and the layout for its content.

    The section starts closed when ``collapsed`` is True and the user has not
    opened it before. The section keeps its open state in the QGIS settings under ``name``. The
    content goes in one inner widget. A collapse hides the direct children of
    the section, so the widgets that the window shows and hides must not be
    direct children.
    """
    box = QgsCollapsibleGroupBox(title)
    box.setCollapsed(collapsed)
    box.setSettingGroup(SETTINGS_GROUP)
    # Set the name after the group. The box reads its saved state when its
    # name changes.
    box.setObjectName(name)
    body = QWidget()
    content = QVBoxLayout(body)
    content.setContentsMargins(0, 0, 0, 0)
    content.setSpacing(8)
    outer = QVBoxLayout(box)
    outer.setContentsMargins(6, 6, 6, 6)
    outer.addWidget(body)
    return box, content


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
        self._server = server
        self._registry_url = registry_url
        self._browser_base = browser_base
        self._entries: list[CatalogEntry] = []
        self._catalog: CatalogEntry | None = None
        self._documents: dict[str, Document] = {}
        self._reading: dict[str, list[Callable[[object, BaseException | None], None]]] = {}
        self._document: Document | None = None
        self._job: DownloadJob | None = None
        self._view_target: Loadable | None = None
        self._load_target: Loadable | None = None
        self._mosaic_busy = False
        # True while a download lists its files, before the job starts.
        self._listing = False
        # The COG file of each item read for the "All files" list, by item href.
        self._item_files: dict[str, Asset | None] = {}
        self.images = ImageCache(self)
        self.world = WorldMap(self)
        self.logomark_file: Path | None = None
        self._build()
        self._restore_layout()
        self.reload()

    # ---------- layout ----------

    def _build(self) -> None:
        self.pages = QStackedWidget()
        self.pages.addWidget(self._registry_page())
        self.pages.addWidget(self._catalog_page())
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.pages)
        self.splitter.addWidget(self._details_panel())
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([400, 600])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._header())
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self._action_bar())
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
        name = QLabel("Portolan")
        font = name.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        name.setFont(font)
        layout.addWidget(name)
        self.tagline = QLabel("Open data registry")
        layout.addWidget(self.tagline)
        layout.addStretch(1)
        # Hidden until the window can build the browser link of a node.
        self.open_browser = QToolButton()
        self.open_browser.setText("Open in browser")
        self.open_browser.setToolTip("See all files and metadata in the Portolan browser")
        self.open_browser.setAutoRaise(True)
        self.open_browser.hide()
        layout.addWidget(self.open_browser)
        self.refresh = QToolButton()
        self.refresh.setIcon(QgsApplication.getThemeIcon("/mActionRefresh.svg"))
        self.refresh.setAutoRaise(True)
        self.refresh.setToolTip("Read the registry again")
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

    def _registry_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 0, 0)
        layout.setSpacing(6)
        self.search = QLineEdit(placeholderText="Search publishers by name or license")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.in_extent = QCheckBox("In map extent")
        self.in_extent.setToolTip("Show only publishers whose data overlaps the map")
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
        self.catalogs.setItemDelegate(CatalogDelegate(self.catalogs, self.images, self.world))
        self.catalogs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
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
        self.back.setText("All publishers")
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
        layout.addWidget(self.tree, 1)
        return page

    def _details_panel(self) -> QScrollArea:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.empty = QLabel("Choose a publisher on the left to see its data here.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color: {muted(self.palette()).name()};")
        layout.addWidget(self.empty, 1)

        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.title.setFont(font)
        layout.addWidget(self.title)

        self.about_box, about = _section("About", "about")
        self.hero = HeroView(self.world)
        about.addWidget(self.hero)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setTextFormat(Qt.TextFormat.MarkdownText)
        self.description.setOpenExternalLinks(True)
        self.description.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        about.addWidget(self.description)
        layout.addWidget(self.about_box)

        self.add_box, add = _section("Add to map", "add")
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.view_button = QPushButton("View on map")
        self.view_button.setIcon(QgsApplication.getThemeIcon("/mActionAddLayer.svg"))
        self._accent(self.view_button)
        self.load_button = QPushButton("Load full data")
        self.load_button.setIcon(QgsApplication.getThemeIcon("/mActionAddOgrLayer.svg"))
        self.mosaic_button = QPushButton("Add as one layer")
        self.mosaic_button.setIcon(QgsApplication.getThemeIcon("/mActionAddRasterLayer.svg"))
        self.mosaic_button.setToolTip(
            "Add the COG files of the items in the chosen area as one raster layer"
        )
        actions.addWidget(self.view_button)
        actions.addWidget(self.load_button)
        actions.addWidget(self.mosaic_button)
        actions.addStretch(1)
        add.addLayout(actions)
        self.tile_styles = QLabel()
        self.tile_styles.setWordWrap(True)
        add.addWidget(self.tile_styles)
        self.scope_row = QWidget()
        scope_layout = QVBoxLayout(self.scope_row)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(4)
        scope_label = QLabel("Area to add or download")
        scope_label.setToolTip(
            "GeoParquet loads, mosaics, and downloads read only the data in this area"
        )
        scope_layout.addWidget(scope_label)
        self.scope_picker = ScopePicker(self._iface)
        scope_layout.addWidget(self.scope_picker)
        self.scope_hint = QLabel()
        self.scope_hint.setWordWrap(True)
        scope_layout.addWidget(self.scope_hint)
        add.addWidget(self.scope_row)
        layout.addWidget(self.add_box)

        # The buttons in "Add to map" cover most needs. The list of every file
        # starts closed. A new setting name lets the closed start reach users
        # who opened the section before.
        self.files_box, files = _section("All files", "all_files", collapsed=True)
        self.assets = QTreeWidget()
        self.assets.setHeaderHidden(True)
        self.assets.setRootIsDecorated(False)
        self.assets.setUniformRowHeights(True)
        self.assets.setFrameShape(QFrame.Shape.NoFrame)
        self.assets.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assets.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.assets.setItemDelegate(AssetDelegate(self.assets))
        self.assets.setToolTip("Select files, then add them to the map or download them")
        files.addWidget(self.assets)
        self.no_assets = QLabel()
        self.no_assets.setWordWrap(True)
        files.addWidget(self.no_assets)
        layout.addWidget(self.files_box)

        self.details_box, details = _section("Details", "details")
        self.facts = QLabel()
        self.facts.setWordWrap(True)
        self.facts.setTextFormat(Qt.TextFormat.RichText)
        self.facts.setOpenExternalLinks(True)
        self.facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        details.addWidget(self.facts)
        layout.addWidget(self.details_box)
        layout.addStretch(1)
        self._hide_actions()
        self._show_sections(False)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _show_sections(self, visible: bool) -> None:
        """Show the title and sections, or the hint when no node is chosen."""
        self.empty.setVisible(not visible)
        self.title.setVisible(visible)
        self.about_box.setVisible(visible)
        self.files_box.setVisible(visible)
        self.details_box.setVisible(visible)
        parts = (
            self.view_button,
            self.load_button,
            self.mosaic_button,
            self.tile_styles,
            self.scope_row,
        )
        self.add_box.setVisible(visible and not all(part.isHidden() for part in parts))

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

    def _action_bar(self) -> QWidget:
        bar = QFrame()
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.add = QPushButton("Add selected")
        self.add.setIcon(QgsApplication.getThemeIcon("/mActionAddLayer.svg"))
        self.add.setToolTip("Add the files selected under All files to the map")
        self.zoom = QPushButton("Zoom to")
        self.zoom.setIcon(QgsApplication.getThemeIcon("/mActionZoomToLayer.svg"))
        self.zoom.setToolTip("Zoom the map to the extent of this catalog, collection, or item")
        self.download = QToolButton()
        self.download.setText("Download")
        self.download.setIcon(QgsApplication.getThemeIcon("/mActionFileSave.svg"))
        self.download.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.download.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.download)
        self.download_selected = menu.addAction("Selected files…")
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
        self.assets.itemClicked.connect(self._asset_clicked)
        self.add.clicked.connect(self._add_selected)
        self.zoom.clicked.connect(self._zoom)
        self.download_selected.triggered.connect(self._download_selected)
        self.download_all.triggered.connect(self._download_all)
        self.cancel.clicked.connect(self._cancel_download)
        self.view_button.clicked.connect(self._view_on_map)
        self.load_button.clicked.connect(self._load_full_data)
        self.mosaic_button.clicked.connect(self._add_mosaic)
        self.open_browser.clicked.connect(self._open_in_browser)
        self.catalogs.customContextMenuRequested.connect(self._catalog_context)
        self.scope_picker.changed.connect(self._sync_buttons)
        project = QgsProject.instance()
        project.layersAdded.connect(self._mark_on_map)
        project.layersRemoved.connect(self._mark_on_map)

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
        self.scope_picker.clear()
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def disconnect_canvas(self) -> None:
        """Stop following the map canvas and the project. Call before the window is deleted."""
        with contextlib.suppress(TypeError):
            self._iface.mapCanvas().extentsChanged.disconnect(self._extent_changed)
        project = QgsProject.instance()
        with contextlib.suppress(TypeError):
            project.layersAdded.disconnect(self._mark_on_map)
        with contextlib.suppress(TypeError):
            project.layersRemoved.disconnect(self._mark_on_map)
        self.scope_picker.clear()

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
        shown = registry.filter_entries(self._entries, self.search.text(), bbox=bbox)
        self.catalogs.clear()
        self._list_catalogs(shown)
        total = len(self._entries)
        if len(shown) == total:
            self.shown.setText(count(total, "publisher"))
        else:
            self.shown.setText(f"{len(shown):,} of {count(total, 'publisher')}")
        self.shown.setStyleSheet(f"color: {muted(self.palette()).name()};")
        if not shown and self._entries:
            self._placeholder("No publisher matches these filters.")

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
            item = QListWidgetItem(more.label(CATALOG_PAGE, "publisher"))
            item.setData(_ROLE, more)
            self.catalogs.addItem(item)

    def _placeholder(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.catalogs.addItem(item)

    def catalog_menu(self, entry: CatalogEntry) -> QMenu:
        """Return the context menu of a publisher row."""
        menu = QMenu(self.catalogs)
        action = menu.addAction("Open in browser")
        link = self.browser_link(entry.url)
        action.setEnabled(link is not None)
        if link is not None:
            action.setToolTip(link)
            action.triggered.connect(lambda: self._open_url(link))
        return menu

    def _catalog_context(self, position: Any) -> None:
        item = self.catalogs.itemAt(position)
        entry = item.data(_ROLE) if item is not None else None
        if isinstance(entry, registry.CatalogEntry):
            self.catalog_menu(entry).exec(self.catalogs.viewport().mapToGlobal(position))

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
        """List ``document``'s children under ``parent``, or at the top of the tree.

        When every child is an item, one row stands for all of them. A
        collection of many image tiles would otherwise fill the tree.
        """
        if not only_items(document):
            self._add_nodes(parent, document.children)
            return
        files = ItemFiles(document.href, document.title, len(document.children))
        row = QTreeWidgetItem([count(files.count, "file")])
        row.setData(0, _ROLE, files)
        row.setIcon(0, _kind_icon("item"))
        row.setToolTip(0, f"Choose {html.escape(document.title)} to add or download these files")
        if parent is None:
            self.tree.addTopLevelItem(row)
        else:
            parent.addChild(row)

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
        if isinstance(node, (Node, ItemFiles)):
            self._choose(node.href, node.title, item)

    def _choose(self, href: str, title: str, item: QTreeWidgetItem | None = None) -> None:
        """Show the document at ``href`` once it is read."""
        self._document = None
        self._view_target = self._load_target = None
        self._show_browser_link()
        self.title.setText(title)
        self.description.setText("Reading…")
        self.facts.clear()
        self.assets.clear()
        self._fit_assets()
        self.hero.show_extent(None)
        self.hero.hide()
        self._show_sections(True)
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

    def _size(self, document: Document) -> int | None:
        """Return the sum of the asset sizes that the document declares.

        A document with no sized assets returns None. When it is the open
        catalog, the registry's total size stands in.
        """
        sizes = [asset.size for asset in document.assets if asset.size is not None]
        if sizes:
            return sum(sizes)
        entry = self._catalog
        if entry is not None and entry.url == document.href:
            return entry.total_size_bytes
        return None

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
        publisher = self._catalog.title if self._catalog is not None else None
        size = self._size(document)
        link = self.browser_link(document.href)
        self.facts.setText(_facts_html(document, publisher, size, muted(self.palette()), link))
        self._show_browser_link()
        self.download_all.setText(f"Everything in {_short(document.title)}…")

        # Only the files QGIS can open show, and an archive that is a link
        # and an asset shows once.
        loadable = document.loadable()
        self.assets.clear()
        for item in loadable:
            row = QTreeWidgetItem([item.title])
            row.setData(0, _ROLE, item)
            asset = item.asset
            if asset is None:
                row.setData(0, BADGE_ROLE, ("PMTiles", "pmtiles", ""))
            else:
                badge = format_label(asset.format, asset.type, asset.href, asset.tabular)
                row.setData(0, BADGE_ROLE, (badge, asset.format, human_size(asset.size)))
            self.assets.addTopLevelItem(row)
        if only_items(document):
            self._list_item_files(document)
        self._mark_on_map()
        self._fit_assets()
        self._select_preferred(document)
        self._show_actions(document, loadable)
        self._show_sections(True)
        self._sync_buttons()

    def _show_actions(self, document: Document, loadable: tuple[Loadable, ...]) -> None:
        """Show the primary buttons, the style note, and the scope picker that fit the document."""
        self._view_target = _first(loadable, VIEW_FORMATS)
        # GeoParquet needs DuckDB. Without it, another format loads in its
        # place. A document with GeoParquet alone keeps it, and the button
        # shows how to install DuckDB.
        formats = LOAD_FORMATS if parquet_query.duckdb_status()[0] else LOAD_FORMATS[1:]
        self._load_target = _first(loadable, formats) or _first(loadable, ("parquet",))
        # A table has no geometry, so it loads whole and says so.
        table = self._load_target is not None and _is_table(self._load_target)
        self.load_button.setText("Load table" if table else "Load full data")
        for button, target, tip in (
            (self.view_button, self._view_target, VIEW_TIP),
            (self.load_button, self._load_target, TABLE_TIP if table else LOAD_TIP),
        ):
            button.setVisible(target is not None)
            if target is not None:
                button.setToolTip(f"{tip}\n{target.title} ({_format_name(target)})")
        self.mosaic_button.setVisible(only_items(document))

        styles = len(document.styles)
        self.tile_styles.setText(
            f"The publisher made {count(styles, 'style')} for these vector tiles. "
            "After you add the tiles, switch styles from the layer's Styles menu."
        )
        self.tile_styles.setStyleSheet(f"color: {muted(self.palette()).name()};")
        self.tile_styles.setVisible(bool(styles and layer_io.archive_urls(document)))
        # The scope limits GeoParquet reads, mosaics, and the walk of a download.
        has_parquet = any(item.format == "parquet" and not _is_table(item) for item in loadable)
        self.scope_row.setVisible(has_parquet or bool(document.children))

    def _hide_actions(self) -> None:
        for widget in (
            self.view_button,
            self.load_button,
            self.mosaic_button,
            self.tile_styles,
            self.scope_row,
        ):
            widget.hide()

    def _list_item_files(self, document: Document) -> None:
        """Add the rows of a collection whose children are all items.

        The first row stands for every file of the items. Add selected
        adds them as one mosaic, and Download downloads those in the
        scope. One page of the items' own COG files follows, after each
        item is read, so the user can pick single files.
        """
        summary = QTreeWidgetItem([count(len(document.children), "file")])
        summary.setData(0, _ROLE, ItemFiles(document.href, document.title, len(document.children)))
        summary.setData(0, BADGE_ROLE, ("COG", "cog", ""))
        summary.setToolTip(
            0,
            "Every file of the items. Add selected adds those in the chosen area as one "
            "layer. Download downloads them.",
        )
        self.assets.addTopLevelItem(summary)
        self._item_files = {}
        self._add_item_rows(document, document.children)

    def _add_item_rows(self, document: Document, nodes: tuple[Node, ...]) -> None:
        """Add one page of item rows, and a row that lists the rest."""
        for node in nodes[:NODE_PAGE]:
            row = QTreeWidgetItem([node.title])
            row.setData(0, BADGE_ROLE, ("COG", "cog", ""))
            row.setData(0, _ROLE, node)
            row.setToolTip(0, "Reading…")
            self.assets.addTopLevelItem(row)
            self._open(node.href, self._item_read(document, row), quiet=True)
        rest = nodes[NODE_PAGE:]
        if rest:
            more = More(rest)
            row = QTreeWidgetItem([more.label(NODE_PAGE, "file")])
            row.setData(0, _ROLE, more)
            row.setForeground(0, QBrush(ACCENT))
            # The row is a button, so a selection skips it.
            row.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.assets.addTopLevelItem(row)
        self._fit_assets()

    def _item_read(
        self, document: Document, row: QTreeWidgetItem
    ) -> Callable[[object, BaseException | None], None]:
        """Return a callback that shows the COG file of a read item in ``row``."""

        def opened(item: object, _error: BaseException | None) -> None:
            if self._document is not document:
                return
            read = item if isinstance(item, Document) else None
            cog = _item_cog(read) if read is not None else None
            if read is None or cog is None:
                # The item has no COG to add, or it cannot be read.
                index = self.assets.indexOfTopLevelItem(row)
                if index >= 0:
                    self.assets.takeTopLevelItem(index)
            else:
                loadable = Loadable(cog.href, read.title, "cog", asset=cog)
                row.setData(0, _ROLE, loadable)
                badge = format_label(cog.format, cog.type, cog.href)
                row.setData(0, BADGE_ROLE, (badge, "cog", human_size(cog.size)))
            if read is not None:
                self._item_files[read.href] = cog
            self._show_item_total(document)
            self._mark_on_map()
            self._fit_assets()
            self._sync_buttons()

        return opened

    def _show_item_total(self, document: Document) -> None:
        """Name the summary row after the COG files, with their size once every item is read."""
        summary = self.assets.topLevelItem(0) if self.assets.topLevelItemCount() else None
        if summary is None or not isinstance(summary.data(0, _ROLE), ItemFiles):
            # A collection with assets of its own lists them first.
            summary = next(
                (
                    self.assets.topLevelItem(i)
                    for i in range(self.assets.topLevelItemCount())
                    if isinstance(self.assets.topLevelItem(i).data(0, _ROLE), ItemFiles)
                ),
                None,
            )
        if summary is None:
            return
        files = list(self._item_files.values())
        total = len(document.children)
        noun = "COG file" if files and all(files) else "file"
        summary.setText(0, count(total, noun))
        sizes = [cog.size for cog in files if cog is not None]
        size = ""
        if len(files) == total and all(files) and all(s is not None for s in sizes):
            size = human_size(sum(s for s in sizes if s is not None))
        summary.setData(0, BADGE_ROLE, ("COG", "cog", size))

    def _asset_clicked(self, row: QTreeWidgetItem | None, _column: int = 0) -> None:
        """List the next page of item files when the user clicks the "Show more" row."""
        document = self._document
        if row is None or document is None or not isinstance(more := row.data(0, _ROLE), More):
            return
        self.assets.takeTopLevelItem(self.assets.indexOfTopLevelItem(row))
        self._add_item_rows(document, more.rest)

    def _select_preferred(self, document: Document) -> None:
        """Select the file that Add selected adds when the user picks nothing else.

        In a collection of items, the row that stands for every file wins.
        """
        href = document.preferred_href(parquet=parquet_query.duckdb_status()[0])
        for i in range(self.assets.topLevelItemCount()):
            row = self.assets.topLevelItem(i)
            if href is None and isinstance(row.data(0, _ROLE), ItemFiles):
                row.setSelected(True)
                return
        if href is None:
            return
        for i in range(self.assets.topLevelItemCount()):
            row = self.assets.topLevelItem(i)
            value = row.data(0, _ROLE)
            if isinstance(value, Loadable) and value.href == href:
                row.setSelected(True)
                return

    def _on_map(self) -> tuple[set[str], list[str]]:
        """Return the file URLs that project layers keep, and the layer sources."""
        urls: set[str] = set()
        sources: list[str] = []
        for layer in QgsProject.instance().mapLayers().values():
            for key in (layer_io.PMTILES_PROPERTY, parquet_layer.SOURCE_PROPERTY):
                value = layer.customProperty(key)
                if value:
                    urls.add(str(value))
            urls.update(mosaic.sources(layer))
            sources.append(layer.source())
        return urls, sources

    def _mark_on_map(self, *_args: object) -> None:
        """Mark the file rows that a layer in the project already reads."""
        urls, sources = self._on_map()
        for i in range(self.assets.topLevelItemCount()):
            row = self.assets.topLevelItem(i)
            item = row.data(0, _ROLE)
            if not isinstance(item, Loadable):
                continue
            path = vsicurl_path(item.href)
            on_map = item.href in urls or any(
                item.href == source or path in source for source in sources
            )
            row.setData(0, ON_MAP_ROLE, on_map)
            row.setToolTip(0, _loadable_tip(item, on_map))

    def _fit_assets(self) -> None:
        """Size the file list to its rows, so the details panel scrolls instead."""
        rows = self.assets.topLevelItemCount()
        self.assets.setVisible(rows > 0)
        self.files_box.setTitle(f"All files ({rows})" if rows else "All files")
        if rows:
            height = sum(self.assets.sizeHintForRow(i) for i in range(rows))
            self.assets.setFixedHeight(height + 2 * self.assets.frameWidth() + 2)
        document = self._document
        text = ""
        if document is not None and only_items(document):
            text = "Select the COG files row to add or download every file in the chosen area."
        elif document is not None and not rows:
            if document.children:
                text = "This entry has no files of its own. Choose a dataset in the tree."
            else:
                text = "This entry lists no files QGIS can open."
        if text:
            self.no_assets.setText(text)
            self.no_assets.setStyleSheet(f"color: {muted(self.palette()).name()};")
            self.no_assets.show()
        else:
            self.no_assets.hide()
        if document is None:
            self._hide_actions()

    def _all_items_selected(self) -> bool:
        """Return whether the row that stands for every file of the items is selected."""
        return any(isinstance(row.data(0, _ROLE), ItemFiles) for row in self.assets.selectedItems())

    def _selected(self) -> tuple[list[str], list[Asset]]:
        tiles: list[str] = []
        assets: list[Asset] = []
        for row in self.assets.selectedItems():
            value = row.data(0, _ROLE)
            if not isinstance(value, Loadable):
                continue
            if value.format == "pmtiles":
                tiles.append(value.href)
            elif value.asset is not None:
                assets.append(value.asset)
        return tiles, assets

    def _scope(self) -> Scope:
        """Return the chosen scope, or Everything when the picker is hidden.

        Raises:
            ScopeError: The choice is not complete.
        """
        if self.scope_row.isHidden():
            return Everything()
        return self.scope_picker.scope()

    def _busy(self) -> bool:
        """Return whether a mosaic search, a download listing, or a download runs."""
        return self._job is not None or self._listing or self._mosaic_busy

    def _refuse_when_busy(self) -> bool:
        """Say why an action waits, and return True, while another task runs."""
        if not self._busy():
            return False
        if self._mosaic_busy:
            what = "the mosaic"
        elif self._listing:
            what = "the list of files to download"
        else:
            what = "the download"
        self._info(f"Wait for {what} to finish, or cancel it.")
        return True

    def _sync_buttons(self) -> None:
        tiles, assets = self._selected()
        every = self._all_items_selected()
        document = self._document
        has_document = document is not None
        self.zoom.setEnabled(document is not None and self._extent(document) is not None)
        problem = None if self.scope_row.isHidden() else self.scope_picker.problem()
        self.scope_hint.setText(problem or "")
        self.scope_hint.setStyleSheet(f"color: {muted(self.palette()).name()};")
        self.scope_hint.setVisible(problem is not None)
        ready = problem is None
        # The mosaic search, the listing, and the download share the progress
        # bar and the Cancel button, so only one of them runs at a time.
        busy = self._busy()
        self.add.setEnabled(bool(tiles or assets) or (every and ready and not busy))
        self.view_button.setEnabled(self._view_target is not None)
        load = self._load_target
        self.load_button.setEnabled(load is not None and (load.format != "parquet" or ready))
        self.mosaic_button.setEnabled(
            document is not None and only_items(document) and ready and not busy
        )
        self.download_selected.setEnabled(bool(tiles or assets or every) and not busy and ready)
        self.download_all.setEnabled(has_document and not busy and ready)
        self.download.setEnabled(
            self.download_selected.isEnabled() or self.download_all.isEnabled()
        )

    def _add_selected(self) -> None:
        document = self._document
        if document is None:
            return
        tiles, assets = self._selected()
        if self._all_items_selected():
            self._add_mosaic()
        if tiles:
            self._add_tiles(document, tiles)
        for asset in assets:
            self._add_file(asset)

    def _add_file(self, asset: Asset) -> None:
        if asset.format == "parquet":
            self._add_parquet(asset)
        else:
            self._add_asset(asset)

    def _view_on_map(self) -> None:
        document, target = self._document, self._view_target
        if document is None or target is None:
            return
        if target.asset is None or target.format == "pmtiles":
            self._add_tiles(document, [target.href])
        else:
            self._add_file(target.asset)

    def _load_full_data(self) -> None:
        target = self._load_target
        if target is not None and target.asset is not None:
            self._add_file(target.asset)

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
            moved = self._add_layers(layers)
            self._info(
                f"Added {len(layers)} vector tile layer(s) from {document.title}.{_ZOOMED * moved}"
            )

        run_task(f"Open vector tiles of {document.title}", prepare, done)

    def _add_asset(self, asset: Asset) -> None:
        try:
            layer = layer_io.asset_layer(asset)
        except layer_io.LayerError as failure:
            self._warn(str(failure))
            return
        moved = self._add_layers([layer])
        self._info(f"Added {asset.label}.{_ZOOMED * moved}")

    def _parquet_scope(self) -> tuple[Scope, dict[str, Any]] | None:
        """Return the scope and the ``parquet_layer.plan`` arguments it gives.

        A map view passes the canvas extent in the canvas CRS, which is
        exact. An area passes its WKT in EPSG:4326. The method warns and
        returns None when the scope is not complete.
        """
        try:
            scope = self._scope()
        except ScopeError as error:
            self._warn(str(error))
            return None
        canvas = self._iface.mapCanvas()
        if isinstance(scope, MapView):
            crs = canvas.mapSettings().destinationCrs()
            return scope, {"extent": canvas.extent(), "extent_crs": crs}
        if isinstance(scope, Area):
            return scope, {"area": scope.wkt}
        return scope, {}

    def _add_parquet(self, asset: Asset) -> None:
        ok, version = parquet_query.duckdb_status()
        if not ok:
            self._duckdb_help(version)
            return
        scoped = self._parquet_scope()
        if scoped is None:
            return
        scope, limits = scoped
        context = QgsProject.instance().transformContext()
        extension_dir = parquet_layer.extension_directory()
        name = _parquet_name(asset)
        in_scope = not isinstance(scope, Everything)
        document = self._document
        has_tiles = document is not None and bool(layer_io.archive_urls(document))

        def read(task: Any) -> parquet_layer.Planned:
            return parquet_layer.plan(
                asset.href,
                context,
                extension_dir=extension_dir,
                cancelled=task.isCanceled,
                **limits,
            )

        def planned(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_layer.Planned):
                if type(error).__name__ == "InterruptException":
                    self._info(f"Stopped reading {name}.")
                else:
                    self._warn(f"DuckDB could not read {asset.href}: {error}")
                return
            # A table has no geometry, so the scope does not limit it.
            limited = in_scope and result.plan.has_geometry
            estimate = result.estimate or 0
            if estimate > CONFIRM_FEATURES and not self._confirm_features(
                name, estimate, limited, has_tiles
            ):
                self._info(f"Did not load {name}.")
                return
            self._copy_parquet(result, name, extension_dir, limited)

        self._info(f"Reading {name} with DuckDB…")
        run_task(f"Read {asset.href} with DuckDB", read, planned)

    def _confirm_features(self, name: str, estimate: int, in_scope: bool, has_tiles: bool) -> bool:
        where = "in the chosen area" if in_scope else "in the file"
        advice = "For a quick look, use View on map instead.\n\n" if has_tiles else ""
        text = (
            f"{name} holds about {estimate:,} features {where}. DuckDB copies them to a "
            "GeoPackage on disk, which takes time and disk space.\n\n"
            f"{advice}Load the features?"
        )
        answer = QMessageBox.question(self, "Load GeoParquet", text)
        return answer == QMessageBox.StandardButton.Yes

    def _copy_parquet(
        self, planned: parquet_layer.Planned, name: str, extension_dir: str, in_scope: bool
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
                    "No feature intersects the chosen area." if in_scope else f"{name} is empty."
                )
                return
            try:
                layer, refused = parquet_layer.build(result, name)
            except OSError as failure:
                self._warn(str(failure))
                return
            moved = self._add_layers([layer])
            unit = "features" if planned.plan.has_geometry else "rows"
            notes = [f"Added {result.written.features:,} {unit} from {name}.{_ZOOMED * moved}"]
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

    def _add_layers(self, layers: list[Any]) -> bool:
        """Add ``layers`` to the project, and show them when the map shows none of them.

        QGIS zooms to a new layer only in an empty project. A layer that
        lands outside the map looks as if it did not load, so the map moves
        to the layers. A table has no extent and does not move the map.

        Returns:
            Whether the map moved.
        """
        project = QgsProject.instance()
        canvas = self._iface.mapCanvas()
        target = canvas.mapSettings().destinationCrs()
        view = canvas.extent()
        union = QgsRectangle()
        union.setNull()
        for layer in layers:
            project.addMapLayer(layer)
        for layer in layers:
            if not layer.isSpatial() or layer.extent().isNull():
                continue
            transform = QgsCoordinateTransform(layer.crs(), target, project)
            try:
                extent = transform.transformBoundingBox(layer.extent())
            except Exception as error:  # noqa: BLE001 - see canvas_bbox
                _log(f"Could not place {layer.name()} on the map: {error}")
                continue
            if extent.intersects(view):
                return False
            union.combineExtentWith(extent)
        if union.isNull():
            return False
        if union.isEmpty():
            # One point has no size, so the map keeps its scale.
            canvas.setCenter(union.center())
        else:
            canvas.setExtent(union.buffered(max(union.width(), union.height()) * 0.05))
        canvas.refresh()
        return True

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

    # ---------- mosaics ----------

    def _add_mosaic(self) -> None:
        """Add the COG files of the open collection's items in the scope as one layer."""
        document = self._document
        if document is None or not only_items(document) or self._refuse_when_busy():
            return
        try:
            scope = self._scope()
        except ScopeError as error:
            self._warn(str(error))
            return
        title = document.title
        self._mosaic_busy = True
        self._sync_buttons()
        self._show_progress(0, 0, f"Finding the files of {title} in the area…")

        def find(task: Any) -> list[str]:
            executor = QtExecutor(download.WALK_WORKERS)
            try:
                return mosaic.item_hrefs_in(
                    document, scope, fetch_json, executor, cancelled=task.isCanceled
                )
            finally:
                executor.shutdown(cancel_futures=True)

        def found(result: object, error: BaseException | None) -> None:
            try:
                searched(result, error)
            except BaseException:
                # The flag must not stay set, or every later click does nothing.
                self._mosaic_done()
                raise

        def searched(result: object, error: BaseException | None) -> None:
            self._hide_progress()
            # A cancelled walk returns the partial list it has. Drop it.
            if task.isCanceled():
                self._mosaic_done()
                self._info(f"Stopped the search for the files of {title}.")
                return
            if not isinstance(result, list):
                self._mosaic_done()
                self._warn(f"Could not list the files of {title}: {error}")
                return
            if not result:
                self._mosaic_done()
                self._info(f"No COG file of {title} lies in the chosen area.")
                return
            if len(result) > CONFIRM_FILES and not self._confirm_mosaic(title, len(result)):
                self._mosaic_done()
                self._info(f"Did not add {title}.")
                return
            self._build_mosaic(result, title)

        task = run_task(f"List the files of {title}", find, found)

    def _confirm_mosaic(self, title: str, files: int) -> bool:
        text = (
            f"{count(files, 'file')} of {title} lie in the chosen area. GDAL opens each "
            "file to build the mosaic, which takes time.\n\n"
            "To add fewer files, draw a smaller area.\n\nAdd the mosaic?"
        )
        answer = QMessageBox.question(self, "Add as one layer", text)
        return answer == QMessageBox.StandardButton.Yes

    def _build_mosaic(self, hrefs: list[str], name: str) -> None:
        def write(task: Any) -> Path:
            return mosaic.write_vrt(hrefs, task.isCanceled)

        def done(result: object, error: BaseException | None) -> None:
            self._mosaic_done()
            if not isinstance(result, Path):
                if isinstance(error, mosaic.MosaicCancelledError):
                    self._info(f"Stopped the mosaic of {name}.")
                else:
                    self._warn(f"Could not build the mosaic of {name}: {error}")
                return
            try:
                layer = mosaic.open_mosaic(result, hrefs, name)
            except layer_io.LayerError as failure:
                self._warn(str(failure))
                return
            moved = self._add_layers([layer])
            self._info(
                f"Added {count(len(hrefs), 'file')} of {name} as one layer.{_ZOOMED * moved}"
            )

        self._info(f"Building a mosaic of {count(len(hrefs), 'file')}…")
        run_task(f"Build a mosaic of {name}", write, done)

    def _mosaic_done(self) -> None:
        self._mosaic_busy = False
        self._sync_buttons()

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

    def _show_browser_link(self) -> None:
        link = self.browser_link(self._browser_target())
        self.open_browser.setVisible(link is not None)
        self.open_browser.setToolTip(f"See all files and metadata in the Portolan browser\n{link}")

    def _open_in_browser(self) -> None:
        link = self.browser_link(self._browser_target())
        if link is not None:
            self._open_url(link)

    @staticmethod
    def _open_url(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    # ---------- downloads ----------

    def _choose_folder(self) -> Path | None:
        folder = QFileDialog.getExistingDirectory(self, "Download into folder")
        return Path(folder) if folder else None

    def _root_url(self) -> str | None:
        return self._catalog.url if self._catalog is not None else None

    def _download_selected(self) -> None:
        """Download the selected files.

        With a map view or an area, each GeoParquet file downloads as a
        GeoPackage that holds only the features in the scope.
        """
        document = self._document
        if document is None or self._refuse_when_busy():
            return
        if self._all_items_selected():
            # The walk of the collection holds every selected file too.
            self._download_all()
            return
        tiles, assets = self._selected()
        clip: list[Asset] = []
        limits: dict[str, Any] = {}
        if any(asset.format == "parquet" for asset in assets):
            scoped = self._parquet_scope()
            if scoped is None:
                return
            scope, limits = scoped
            if not isinstance(scope, Everything):
                clip = [asset for asset in assets if asset.format == "parquet"]
                assets = [asset for asset in assets if asset.format != "parquet"]
        if clip:
            ok, version = parquet_query.duckdb_status()
            if not ok:
                self._duckdb_help(version)
                return
        root = self._root_url() or document.href
        files = [
            download.PlannedFile(a.href, download.local_path(a.href, root), a.size, a.checksum)
            for a in assets
        ]
        files += [download.PlannedFile(url, download.local_path(url, root)) for url in tiles]
        folder = self._choose_folder()
        if folder is None:
            return
        if files:
            self._start_download(folder, files)
        for asset in clip:
            self._download_clipped(asset, folder / clipped_path(asset.href, root), limits)

    def _download_clipped(self, asset: Asset, target: Path, limits: dict[str, Any]) -> None:
        """Write the features of a GeoParquet file in the scope to a GeoPackage at ``target``.

        A file with no geometry column has nothing to clip, so the copy
        holds every row.
        """
        context = QgsProject.instance().transformContext()
        extension_dir = parquet_layer.extension_directory()
        name = _parquet_name(asset)

        def write(task: Any) -> parquet_query.Written:
            planned = parquet_layer.plan(
                asset.href,
                context,
                extension_dir=extension_dir,
                cancelled=task.isCanceled,
                **limits,
            )
            prepared = parquet_layer.prepare(planned, extension_dir, cancelled=task.isCanceled)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(prepared.path), str(target))
            finally:
                parquet_layer.discard_path(prepared.path)
            return prepared.written

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_query.Written):
                if type(error).__name__ == "InterruptException":
                    self._info(f"Stopped the download of {name}.")
                else:
                    self._warn(f"Could not write {target}: {error}")
                return
            self._info(f"Wrote {result.features:,} features of {name} to {target}.")

        self._info(f"Writing the features of {name} in the chosen area to {target}…")
        run_task(f"Write {name} in the chosen area", write, done)

    def _download_all(self) -> None:
        document = self._document
        if document is None or self._refuse_when_busy():
            return
        try:
            scope = self._scope()
        except ScopeError as error:
            self._warn(str(error))
            return
        folder = self._choose_folder()
        if folder is None:
            return
        root = self._root_url() or document.href
        self._listing = True
        self._sync_buttons()
        self._show_progress(0, 0, f"Listing everything below {document.title}…")

        def build(task: Any) -> download.Plan:
            executor = QtExecutor(download.WALK_WORKERS)
            try:
                return download.plan(
                    fetch_json,
                    document.href,
                    root,
                    cancelled=task.isCanceled,
                    executor=executor,
                    scope=scope,
                )
            finally:
                executor.shutdown(cancel_futures=True)

        def planned(result: object, error: BaseException | None) -> None:
            self._listing = False
            self._hide_progress()
            self._sync_buttons()
            # A cancelled walk returns the partial plan it has. Drop it.
            if task.isCanceled():
                self._info(f"Listing of {document.title} cancelled.")
                return
            if not isinstance(result, download.Plan):
                self._warn(f"Could not list {document.title}: {error}")
                return
            if self._confirm(result):
                self._start_download(folder, result.files)

        task = run_task(f"List {document.title}", build, planned)

    def _confirm(self, plan: download.Plan) -> bool:
        lines = [f"{len(plan.files)} files, at least {human_size(plan.known_bytes)}."]
        if plan.unknown_sizes:
            lines.append(f"{plan.unknown_sizes} files do not declare a size.")
        if plan.failures:
            lines.append(f"{len(plan.failures)} documents could not be read and are left out.")
        if plan.truncated:
            lines.append("The listing stopped at its document limit.")
        if plan.outside:
            lines.append(f"{count(plan.outside, 'item')} outside the chosen area are left out.")
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


def _item_cog(item: Document) -> Asset | None:
    """Return the first COG data asset of an item."""
    return next((asset for asset in item.data_assets if asset.format == "cog"), None)


def _is_table(item: Loadable) -> bool:
    """Return whether ``item`` is a Parquet file with no geometry column."""
    return item.asset is not None and item.asset.tabular


def _format_name(item: Loadable) -> str:
    """Return the format of a file in plain words, such as "GeoParquet"."""
    asset = item.asset
    if asset is None:
        return "PMTiles"
    return format_label(asset.format, asset.type, asset.href, asset.tabular)


def _loadable_tip(item: Loadable, on_map: bool = False) -> str:
    """Return the tooltip of a file row."""
    lines = [html.escape(item.href)]
    if item.layers:
        lines.append(f"Layers: {html.escape(', '.join(item.layers))}")
    if item.asset is not None and item.asset.roles:
        lines.append(f"Roles: {html.escape(', '.join(item.asset.roles))}")
    if on_map:
        lines.append("A layer in the project already reads this file.")
    return "<br>".join(lines)


def _parquet_name(asset: Asset) -> str:
    """Return the layer name of a GeoParquet file."""
    return asset.title or asset.href.rsplit("/", 1)[-1].removesuffix(".parquet")


def clipped_path(href: str, root: str) -> str:
    """Return where the GeoPackage of a clipped GeoParquet file goes in the download folder.

    It keeps the file's place in the catalog layout, with a ``.gpkg`` suffix.
    """
    path = download.local_path(href, root)
    folder, _, name = path.rpartition("/")
    stem = name
    for suffix in (".geoparquet", ".parquet"):
        if name.lower().endswith(suffix):
            stem = name[: -len(suffix)]
            break
    return f"{folder}/{stem}.gpkg" if folder else f"{stem}.gpkg"


def _facts_html(
    document: Document,
    publisher: str | None,
    size: int | None,
    label_color: QColor,
    browser: str | None = None,
) -> str:
    """Return the document's facts as one two-column table.

    Each fact is one row, and a row leaves out what is not known. The
    license links make the License row, the publisher's documentation makes
    the Documentation row, the ``via`` links make the Source row, and the
    browser page makes the More row. A link
    that two STAC links repeat shows once.
    """

    def anchor(href: str, text: str) -> str:
        return f'<a href="{html.escape(href)}">{html.escape(text)}</a>'

    def hrefs(rel: str) -> list[tuple[str, str | None]]:
        found: dict[str, str | None] = {}
        for link in document.links:
            if link.rel == rel and link.href not in found:
                found[link.href] = link.title
        return list(found.items())

    rows: list[tuple[str, str]] = []
    if publisher:
        rows.append(("Publisher", html.escape(publisher)))
    name = license_name(document.license)
    licenses = hrefs("license")
    if len(licenses) == 1:
        rows.append(("License", anchor(licenses[0][0], name or link_label(*licenses[0]))))
    elif licenses:
        linked = ", ".join(anchor(href, link_label(href, title)) for href, title in licenses)
        rows.append(("License", f"{html.escape(name)}: {linked}" if name else linked))
    elif name:
        rows.append(("License", html.escape(name)))
    if size:
        rows.append(("Size", html.escape(human_size(size))))
    docs = hrefs("describedby")
    if docs:
        rows.append(
            (
                "Documentation",
                ", ".join(anchor(href, link_label(href, title)) for href, title in docs),
            )
        )
    sources = hrefs("via")
    if sources:
        rows.append(
            ("Source", ", ".join(anchor(href, link_label(href, title)) for href, title in sources))
        )
    if browser:
        rows.append(("More", anchor(browser, BROWSER_LINK)))
    color = label_color.name()
    cells = "".join(
        f'<tr><td style="color: {color}; padding-right: 10px;">{html.escape(key)}</td>'
        f"<td>{value}</td></tr>"
        for key, value in rows
    )
    return f"<table>{cells}</table>"
