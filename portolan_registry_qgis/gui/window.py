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
from dataclasses import dataclass, replace
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
    QAction,
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
    format_label,
    human_size,
    license_name,
    link_label,
)
from portolan_registry_qgis.core.hrefs import (
    BROWSER_URL,
    browser_url,
    plain_vsicurl_path,
    vsicurl_path,
)
from portolan_registry_qgis.core.i18n import N_, _, count, ngettext, number
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
VIEW_TIP = N_("Fast. Draws with the publisher's style.")
LOAD_TIP = N_("All features and attributes, for analysis.")
TABLE_TIP = N_("All rows and columns, as a table with no geometry.")
# What each button adds, by format. The tooltip leads with it.
VIEW_KINDS = {
    "pmtiles": N_("Adds the vector tiles (PMTiles). They read only the tiles on screen."),
    "cog": N_("Adds the raster (Cloud Optimized GeoTIFF), read in place over HTTP."),
}
LOAD_KINDS = {
    "parquet": N_("Loads the GeoParquet. DuckDB copies the features in the area of interest."),
    "flatgeobuf": N_("Loads the FlatGeobuf, read in place over HTTP."),
    "geojson": N_("Loads the GeoJSON, read in place over HTTP."),
    "csv": N_("Loads the CSV, read in place over HTTP."),
}
# Marks a link that leaves QGIS.
EXTERNAL = "\u2197"
VIEW_FORMATS = ("pmtiles", "cog")
LOAD_FORMATS = ("parquet", "flatgeobuf", "geojson", "csv")
BROWSER_LINK = N_("All files and metadata in the Portolan browser")
_KIND_ICONS = {
    "catalog": "mIconFolder",
    "collection": "mIconLayerTree",
    "item": "mIconFile",
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


def _zoomed(message: str, moved: bool) -> str:
    """Return ``message``, and the note that the map moved when ``moved`` is True."""
    return f"{message} {_('The map moved to show it.')}" if moved else message


def _open_label() -> str:
    """Return the text of the action that opens the Portolan browser."""
    # Translators: {arrow} is an arrow that marks a link that leaves QGIS.
    return _("Open in Portolan browser {arrow}").format(arrow=EXTERNAL)


def _theme_icon(name: str) -> QIcon:
    """Return the QGIS theme icon ``name``, such as "mActionAddLayer"."""
    return QgsApplication.getThemeIcon(f"/{name}.svg")


def _kind_icon(kind: str) -> QIcon:
    return _theme_icon(_KIND_ICONS.get(kind, _KIND_ICONS["item"]))


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
        self.refresh.setIcon(_theme_icon("mActionRefresh"))
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

    def _registry_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
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
        self.back.setText(_("All publishers"))
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

        self.empty = QLabel(_("Choose a publisher on the left to see its data here."))
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
        # The buttons sit next to the name of the open node, so they read as
        # actions on it, not on the registry.
        self.title_row = QWidget()
        title_layout = QHBoxLayout(self.title_row)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(4)
        title_layout.addWidget(self.title, 1)
        self.zoom = QToolButton()
        # Translators: a button that zooms the map to the dataset's extent.
        self.zoom.setText(_("Zoom to"))
        self.zoom.setIcon(_theme_icon("mActionZoomToLayer"))
        self.zoom.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.zoom.setAutoRaise(True)
        self.zoom.setToolTip(_("Zoom the map to the extent of this dataset"))
        title_layout.addWidget(self.zoom, 0, Qt.AlignmentFlag.AlignTop)
        # Hidden until the window can build the browser link of a node.
        self.open_browser = QToolButton()
        self.open_browser.setText(_open_label())
        self.open_browser.setAutoRaise(True)
        self.open_browser.hide()
        title_layout.addWidget(self.open_browser, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.title_row)

        # Translators: the title of the section that describes the dataset.
        self.about_box, about = _section(_("About"), "about")
        self.hero = HeroView(self.world)
        about.addWidget(self.hero)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setTextFormat(Qt.TextFormat.MarkdownText)
        self.description.setOpenExternalLinks(True)
        self.description.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        about.addWidget(self.description)
        layout.addWidget(self.about_box)

        # Every action in this section reads only the data in the area, so
        # the area comes first.
        self.add_box, add = _section(_("Add to map"), "add")
        self.scope_row = QWidget()
        scope_layout = QVBoxLayout(self.scope_row)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(4)
        scope_label = QLabel(_("Area of interest"))
        scope_label.setToolTip(
            _(
                "GeoParquet loads, mosaics, and downloads read only the data in this area. "
                "A mosaic shows only the pixels inside it."
            )
        )
        scope_layout.addWidget(scope_label)
        self.scope_picker = ScopePicker(self._iface)
        scope_layout.addWidget(self.scope_picker)
        self.scope_hint = QLabel()
        self.scope_hint.setWordWrap(True)
        scope_layout.addWidget(self.scope_hint)
        add.addWidget(self.scope_row)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.view_button = QPushButton(_("View on map"))
        self.view_button.setIcon(_theme_icon("mActionAddLayer"))
        self._accent(self.view_button)
        self.load_button = QPushButton(_("Load full data"))
        self.load_button.setIcon(_theme_icon("mActionAddOgrLayer"))
        self.mosaic_button = QPushButton(_("Add as one layer"))
        self.mosaic_button.setIcon(_theme_icon("mActionAddRasterLayer"))
        self.mosaic_button.setToolTip(
            _("Add the COG files in the area as one raster layer, clipped to the area")
        )
        self._accent(self.mosaic_button)
        # Translators: a button that asks where to download the files.
        download_label = _("Download…")
        self.download_all = QAction(_theme_icon("mActionFileSave"), download_label, self)
        self.download_area = QToolButton()
        self.download_area.setDefaultAction(self.download_all)
        self.download_area.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        actions.addWidget(self.view_button)
        actions.addWidget(self.load_button)
        actions.addWidget(self.mosaic_button)
        actions.addWidget(self.download_area)
        actions.addStretch(1)
        add.addLayout(actions)
        self.tile_styles = QLabel()
        self.tile_styles.setWordWrap(True)
        add.addWidget(self.tile_styles)
        layout.addWidget(self.add_box)

        # The buttons in "Add to map" cover most needs. The list of every file
        # starts closed. A new setting name lets the closed start reach users
        # who opened the section before.
        self.files_box, files = _section(_("All files"), "all_files", collapsed=True)
        self.assets = QTreeWidget()
        self.assets.setHeaderHidden(True)
        self.assets.setRootIsDecorated(False)
        self.assets.setUniformRowHeights(True)
        self.assets.setFrameShape(QFrame.Shape.NoFrame)
        self.assets.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assets.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.assets.setItemDelegate(AssetDelegate(self.assets))
        self.assets.setToolTip(_("Select files, then add them to the map or download them"))
        files.addWidget(self.assets)
        self.no_assets = QLabel()
        self.no_assets.setWordWrap(True)
        files.addWidget(self.no_assets)
        # These buttons act on the selected rows alone, whatever the area.
        file_actions = QHBoxLayout()
        file_actions.setSpacing(6)
        self.add = QPushButton(_("Add selected"))
        self.add.setIcon(_theme_icon("mActionAddLayer"))
        self.add.setToolTip(_("Add the selected files to the map, whole"))
        self.download_selected = QAction(
            _theme_icon("mActionFileSave"),
            _("Download selected…"),
            self,
        )
        self.download_files = QToolButton()
        self.download_files.setDefaultAction(self.download_selected)
        self.download_files.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        file_actions.addWidget(self.add)
        file_actions.addWidget(self.download_files)
        file_actions.addStretch(1)
        files.addLayout(file_actions)
        layout.addWidget(self.files_box)

        self.details_box, details = _section(_("Details"), "details")
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
        self.title_row.setVisible(visible)
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
        """Return the bar that shows the progress of a running task, with Cancel."""
        self.task_bar = QFrame()
        self.task_bar.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self.task_bar)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.hide()
        self.cancel = QPushButton(_("Cancel"))
        self.cancel.hide()
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel)
        layout.addLayout(progress_row)
        self.task_bar.hide()
        return self.task_bar

    @staticmethod
    def _accent(button: QPushButton) -> None:
        """Paint the primary action in the Portolan blue."""
        hover = mix(ACCENT, QColor("#16170f"), 0.18).name()
        accent = ACCENT.name()
        button.setStyleSheet(
            f"QPushButton {{ background: {accent}; color: #fcfcfa; border: 1px solid {hover};"  # i18n: skip
            " border-radius: 3px; padding: 4px 12px; font-weight: bold; }"
            f"QPushButton:hover {{ background: {hover}; }}"  # i18n: skip
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
        self.assets.itemDoubleClicked.connect(lambda *_args: self._add_selected())
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
        self.scope_picker.release()

    # ---------- registry ----------

    def reload(self) -> None:
        """Read the registry and list its catalogs."""
        self.catalogs.clear()
        self.shown.clear()
        self._placeholder(_("Reading the Portolan registry…"))
        url = self._registry_url

        def read(_task: object) -> list[CatalogEntry]:
            return registry.parse_registry(fetch_json(url), url)

        run_task(_("Read the Portolan registry"), read, self._registry_read)

    def _registry_read(self, result: object, error: BaseException | None) -> None:
        if error is not None or not isinstance(result, list):
            self.catalogs.clear()
            self._placeholder(
                _("Could not read the Portolan registry. Check the connection, then reload.")
            )
            self._warn(_("Could not read the Portolan registry: {error}").format(error=error))
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
        action = menu.addAction(_open_label())
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
        row = QTreeWidgetItem([count("{n} file", "{n} files", files.count)])
        row.setData(0, _ROLE, files)
        row.setIcon(0, _kind_icon("item"))
        tip = _("Choose {title} to add or download these files")
        row.setToolTip(0, tip.format(title=html.escape(document.title)))
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
            row.setToolTip(
                0, count("This level lists {n} more", "This level lists {n} more", len(rest))
            )
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

        # Translators: a task in the QGIS task manager that reads the document at {url}.
        run_task(_("Read {url}").format(url=href), read, done, hidden=quiet)

    def _expand(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, _ROLE)
        if not isinstance(node, Node) or item.data(1, _ROLE):
            return
        item.setData(1, _ROLE, True)
        loading = QTreeWidgetItem([_("Reading…")])
        loading.setFlags(Qt.ItemFlag.NoItemFlags)
        item.addChild(loading)

        def opened(document: object, error: BaseException | None) -> None:
            item.takeChildren()
            if not isinstance(document, Document):
                item.setData(1, _ROLE, False)
                failed = QTreeWidgetItem([_("Could not read: {error}").format(error=error)])
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
        self.description.setText(_("Reading…"))
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
        self.description.setText(_("Could not read {url}: {error}").format(url=href, error=error))
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
        self.download_all.setToolTip(
            _("Download the files of {title} in the area").format(title=document.title)
        )

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
        self.load_button.setText(_("Load table") if table else _("Load full data"))
        for button, target, tip in (
            (self.view_button, self._view_target, VIEW_TIP),
            (self.load_button, self._load_target, TABLE_TIP if table else LOAD_TIP),
        ):
            button.setVisible(target is not None)
            if target is not None:
                kinds = VIEW_KINDS if button is self.view_button else LOAD_KINDS
                kind = kinds.get(target.format)
                lines = [_(kind) if kind else ""]
                if table and button is self.load_button:
                    lines = [_("Loads the Parquet table. It has no geometry, so it loads whole.")]
                # Translators: {title} names a file, and {format} its format, such as GeoParquet.
                file_line = _("File: {title} ({format})")
                lines += [_(tip), file_line.format(title=target.title, format=_format_name(target))]
                button.setToolTip("\n".join(line for line in lines if line))
        self.mosaic_button.setVisible(only_items(document))

        styles = len(document.styles)
        self.tile_styles.setText(
            count(
                "The publisher made {n} style for these vector tiles. "
                "After you add the tiles, switch styles from the layer's Styles menu.",
                "The publisher made {n} styles for these vector tiles. "
                "After you add the tiles, switch styles from the layer's Styles menu.",
                styles,
            )
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
        """Add the COG file of each item, one page at a time, after each item is read.

        The rows let the user pick single files. **Add as one layer** in
        "Add to map" covers every file in the area.
        """
        self._item_files = {}
        self._add_item_rows(document, document.children)

    def _add_item_rows(self, document: Document, nodes: tuple[Node, ...]) -> None:
        """Add one page of item rows, and a row that lists the rest."""
        for node in nodes[:NODE_PAGE]:
            row = QTreeWidgetItem([node.title])
            row.setData(0, BADGE_ROLE, ("COG", "cog", ""))
            row.setData(0, _ROLE, node)
            row.setToolTip(0, _("Reading…"))
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
            self._mark_on_map()
            self._fit_assets()
            self._sync_buttons()

        return opened

    def _asset_clicked(self, row: QTreeWidgetItem | None, _column: int = 0) -> None:
        """List the next page of item files when the user clicks the "Show more" row."""
        document = self._document
        if row is None or document is None or not isinstance(more := row.data(0, _ROLE), More):
            return
        self.assets.takeTopLevelItem(self.assets.indexOfTopLevelItem(row))
        self._add_item_rows(document, more.rest)

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
            # The path form depends on the GDAL settings of the session.
            paths = (vsicurl_path(item.href), plain_vsicurl_path(item.href))
            on_map = item.href in urls or any(
                item.href == source or any(path in source for path in paths) for source in sources
            )
            row.setData(0, ON_MAP_ROLE, on_map)
            row.setToolTip(0, _loadable_tip(item, on_map))

    def _fit_assets(self) -> None:
        """Size the file list to its rows, so the details panel scrolls instead."""
        rows = self.assets.topLevelItemCount()
        self.assets.setVisible(rows > 0)
        self.files_box.setTitle(
            _("All files ({count})").format(count=number(rows)) if rows else _("All files")
        )
        if rows:
            height = sum(self.assets.sizeHintForRow(i) for i in range(rows))
            self.assets.setFixedHeight(height + 2 * self.assets.frameWidth() + 2)
        document = self._document
        text = ""
        if document is not None and only_items(document):
            text = _(
                "To add every file in the area as one layer, use Add as one layer above. "
                "Select files here to add or download them whole."
            )
        elif document is not None and not rows:
            if document.children:
                text = _("This entry has no files of its own. Choose a dataset in the tree.")
            else:
                text = _("This entry lists no files QGIS can open.")
        if text:
            self.no_assets.setText(text)
            self.no_assets.setStyleSheet(f"color: {muted(self.palette()).name()};")
            self.no_assets.show()
        else:
            self.no_assets.hide()
        if document is None:
            self._hide_actions()

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
            self._info(_("Wait for the mosaic to finish, or cancel it."))
        elif self._listing:
            self._info(_("Wait for the list of files to download to finish, or cancel it."))
        else:
            self._info(_("Wait for the download to finish, or cancel it."))
        return True

    def _sync_buttons(self) -> None:
        tiles, assets = self._selected()
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
        # The All files buttons act on the selected rows, whole, so the area
        # does not hold them back.
        self.add.setEnabled(bool(tiles or assets))
        self.view_button.setEnabled(self._view_target is not None)
        load = self._load_target
        self.load_button.setEnabled(load is not None and (load.format != "parquet" or ready))
        self.mosaic_button.setEnabled(
            document is not None and only_items(document) and ready and not busy
        )
        self.download_selected.setEnabled(bool(tiles or assets) and not busy)
        self.download_all.setEnabled(has_document and not busy and ready)

    def _add_selected(self) -> None:
        document = self._document
        if document is None:
            return
        tiles, assets = self._selected()
        if tiles:
            self._add_tiles(document, tiles)
        for asset in assets:
            self._add_file(asset, whole=True)

    def _add_file(self, asset: Asset, whole: bool = False) -> None:
        """Add one file. ``whole`` skips the area, as the All files buttons do."""
        if asset.format == "parquet":
            self._add_parquet(asset, whole)
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
                self._warn(_("Could not add vector tiles: {error}").format(error=error))
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
            added = ngettext(
                "Added {n} vector tile layer from {title}.",
                "Added {n} vector tile layers from {title}.",
                len(layers),
            )
            self._info(_zoomed(added.format(n=number(len(layers)), title=document.title), moved))

        run_task(_("Open vector tiles of {title}").format(title=document.title), prepare, done)

    def _add_asset(self, asset: Asset) -> None:
        try:
            layer = layer_io.asset_layer(asset)
        except layer_io.LayerError as failure:
            self._warn(str(failure))
            return
        moved = self._add_layers([layer])
        self._info(_zoomed(_("Added {name}.").format(name=asset.label), moved))

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

    def _add_parquet(self, asset: Asset, whole: bool = False) -> None:
        ok, version = parquet_query.duckdb_status()
        if not ok:
            self._duckdb_help(version)
            return
        scoped: tuple[Scope, dict[str, Any]] | None = (Everything(), {})
        if not whole:
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
                    self._info(_("Stopped reading {name}.").format(name=name))
                else:
                    self._warn(
                        _("DuckDB could not read {url}: {error}").format(
                            url=asset.href, error=error
                        )
                    )
                return
            # A table has no geometry, so the scope does not limit it.
            limited = in_scope and result.plan.has_geometry
            estimate = result.estimate or 0
            if estimate > CONFIRM_FEATURES and not self._confirm_features(
                name, estimate, limited, has_tiles
            ):
                self._info(_("Did not load {name}.").format(name=name))
                return
            self._copy_parquet(result, name, extension_dir, limited)

        self._info(_("Reading {name} with DuckDB…").format(name=name))
        run_task(_("Read {url} with DuckDB").format(url=asset.href), read, planned)

    def _confirm_features(self, name: str, estimate: int, in_scope: bool, has_tiles: bool) -> bool:
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
        answer = QMessageBox.question(self, _("Load GeoParquet"), "\n\n".join(paragraphs))
        return answer == QMessageBox.StandardButton.Yes

    def _copy_parquet(
        self, planned: parquet_layer.Planned, name: str, extension_dir: str, in_scope: bool
    ) -> None:
        def copy(task: Any) -> parquet_layer.Prepared:
            return parquet_layer.prepare(planned, extension_dir, cancelled=task.isCanceled)

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_layer.Prepared):
                if type(error).__name__ == "InterruptException":
                    self._info(_("Stopped the copy of {name}.").format(name=name))
                else:
                    self._warn(
                        _("DuckDB could not read {url}: {error}").format(
                            url=planned.url, error=error
                        )
                    )
                return
            if not result.written.features:
                parquet_layer.discard_path(result.path)
                self._info(
                    _("No feature intersects the chosen area.")
                    if in_scope
                    else _("{name} is empty.").format(name=name)
                )
                return
            try:
                layer, refused = parquet_layer.build(result, name)
            except OSError as failure:
                self._warn(str(failure))
                return
            moved = self._add_layers([layer])
            features = result.written.features
            if planned.plan.has_geometry:
                added = ngettext(
                    "Added {n} feature from {name}.", "Added {n} features from {name}.", features
                )
            else:
                added = ngettext(
                    "Added {n} row from {name}.", "Added {n} rows from {name}.", features
                )
            notes = [_zoomed(added.format(n=number(features), name=name), moved)]
            if refused:
                # Translators: {geometry_type} is a geometry type, such as Point or Polygon.
                no_geometry = ngettext(
                    "{n} of them has no geometry, because its geometry type "
                    "does not fit a {geometry_type} layer.",
                    "{n} of them have no geometry, because their geometry type "
                    "does not fit a {geometry_type} layer.",
                    refused,
                )
                notes.append(
                    no_geometry.format(
                        n=number(refused), geometry_type=result.written.geometry_type
                    )
                )
            (self._warn if refused else self._info)(" ".join(notes))

        if planned.estimate is not None:
            copying = ngettext(
                "Copying about {n} feature of {name} with DuckDB…",
                "Copying about {n} features of {name} with DuckDB…",
                planned.estimate,
            ).format(n=number(planned.estimate), name=name)
        else:
            copying = _("Copying the features of {name} with DuckDB…").format(name=name)
        self._info(copying)
        run_task(_("Copy {url} with DuckDB").format(url=planned.url), copy, done)

    def _duckdb_help(self, version: str | None) -> None:
        minimum = ".".join(str(part) for part in parquet_query.MINIMUM_DUCKDB)
        if version:
            heading = _("DuckDB {version} is too old. GeoParquet needs {minimum} or newer.").format(
                version=html.escape(version), minimum=minimum
            )
        else:
            heading = _(
                "GeoParquet needs DuckDB {minimum} or newer, and it is not installed."
            ).format(minimum=minimum)
        command = html.escape(parquet_query.install_command(upgrade=version is not None))
        where = (
            _("QGIS runs in a Flatpak, so install DuckDB into your user folder from a terminal:")
            if parquet_query.is_flatpak()
            else _("Install it from a terminal with the Python that QGIS uses:")
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(_("Portolan Registry: DuckDB needed"))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<p><b>{heading}</b></p><p>{where}</p><pre>{command}</pre>"
            f"<p>{_('Then restart QGIS.')}</p>"
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
                _log(
                    _("Could not place {name} on the map: {error}").format(
                        name=layer.name(), error=error
                    )
                )
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
            self._warn(_("Could not transform the extent: {error}").format(error=error))
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
        self._show_progress(
            0, 0, _("Finding the files of {title} in the area…").format(title=title)
        )

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
                self._info(_("Stopped the search for the files of {title}.").format(title=title))
                return
            if not isinstance(result, list):
                self._mosaic_done()
                self._warn(
                    _("Could not list the files of {title}: {error}").format(
                        title=title, error=error
                    )
                )
                return
            if not result:
                self._mosaic_done()
                self._info(_("No COG file of {title} lies in the chosen area.").format(title=title))
                return
            if len(result) > CONFIRM_FILES and not self._confirm_mosaic(title, len(result)):
                self._mosaic_done()
                self._info(_("Did not add {title}.").format(title=title))
                return
            self._build_mosaic(result, title, _clip_of(scope))

        task = run_task(_("List the files of {title}").format(title=title), find, found)

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
        answer = QMessageBox.question(self, _("Add as one layer"), "\n\n".join(paragraphs))
        return answer == QMessageBox.StandardButton.Yes

    def _build_mosaic(self, hrefs: list[str], name: str, clip: str | None = None) -> None:
        def write(task: Any) -> Path:
            return mosaic.write_vrt(hrefs, task.isCanceled, clip)

        def done(result: object, error: BaseException | None) -> None:
            self._mosaic_done()
            if not isinstance(result, Path):
                if isinstance(error, mosaic.MosaicCancelledError):
                    self._info(_("Stopped the mosaic of {name}.").format(name=name))
                else:
                    self._warn(
                        _("Could not build the mosaic of {name}: {error}").format(
                            name=name, error=error
                        )
                    )
                return
            try:
                layer = mosaic.open_mosaic(result, hrefs, name, clip)
            except layer_io.LayerError as failure:
                self._warn(str(failure))
                return
            moved = self._add_layers([layer])
            added = ngettext(
                "Added {n} file of {name} as one layer.",
                "Added {n} files of {name} as one layer.",
                len(hrefs),
            )
            self._info(_zoomed(added.format(n=number(len(hrefs)), name=name), moved))

        self._info(
            count("Building a mosaic of {n} file…", "Building a mosaic of {n} files…", len(hrefs))
        )
        run_task(_("Build a mosaic of {name}").format(name=name), write, done)

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
        tip = _("See every file and all metadata of this dataset")
        self.open_browser.setToolTip(f"{tip}\n{link}")

    def _open_in_browser(self) -> None:
        link = self.browser_link(self._browser_target())
        if link is not None:
            self._open_url(link)

    @staticmethod
    def _open_url(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    # ---------- downloads ----------

    def _choose_folder(self) -> Path | None:
        folder = QFileDialog.getExistingDirectory(self, _("Download into folder"))
        return Path(folder) if folder else None

    def _root_url(self) -> str | None:
        return self._catalog.url if self._catalog is not None else None

    def _download_selected(self) -> None:
        """Download the selected files, whole, whatever the area."""
        document = self._document
        if document is None or self._refuse_when_busy():
            return
        tiles, assets = self._selected()
        root = self._root_url() or document.href
        files = [
            download.PlannedFile(a.href, download.local_path(a.href, root), a.size, a.checksum)
            for a in assets
        ]
        files += [download.PlannedFile(url, download.local_path(url, root)) for url in tiles]
        if not files:
            return
        folder = self._choose_folder()
        if folder is not None:
            self._start_download(folder, files)

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
                    self._info(_("Stopped the download of {name}.").format(name=name))
                else:
                    self._warn(
                        _("Could not write {path}: {error}").format(path=target, error=error)
                    )
                return
            wrote = ngettext(
                "Wrote {n} feature of {name} to {path}.",
                "Wrote {n} features of {name} to {path}.",
                result.features,
            )
            self._info(wrote.format(n=number(result.features), name=name, path=target))

        self._info(
            _("Writing the features of {name} in the chosen area to {path}…").format(
                name=name, path=target
            )
        )
        run_task(_("Write {name} in the chosen area").format(name=name), write, done)

    def _download_all(self) -> None:
        """Download everything in the area.

        The walk leaves out the items outside the area. With a map view or
        an area, each GeoParquet file of the document downloads as a
        GeoPackage of the features in the area.
        """
        document = self._document
        if document is None or self._refuse_when_busy():
            return
        scoped = self._parquet_scope()
        if scoped is None:
            return
        scope, limits = scoped
        clip: list[Asset] = []
        if not isinstance(scope, Everything):
            clip = [
                item.asset
                for item in document.loadable()
                if item.asset is not None and item.format == "parquet" and not item.asset.tabular
            ]
        if clip and not parquet_query.duckdb_status()[0]:
            self._duckdb_help(parquet_query.duckdb_status()[1])
            return
        folder = self._choose_folder()
        if folder is None:
            return
        root = self._root_url() or document.href
        self._listing = True
        self._sync_buttons()
        self._show_progress(
            0, 0, _("Listing everything below {title}…").format(title=document.title)
        )

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
                self._info(_("Listing of {title} cancelled.").format(title=document.title))
                return
            if not isinstance(result, download.Plan):
                self._warn(
                    _("Could not list {title}: {error}").format(title=document.title, error=error)
                )
                return
            clipped = {asset.href for asset in clip}
            files = [entry for entry in result.files if entry.url not in clipped]
            if not self._confirm(replace(result, files=files), len(clip)):
                return
            if files:
                self._start_download(folder, files)
            for asset in clip:
                self._download_clipped(asset, folder / clipped_path(asset.href, root), limits)

        # Translators: a task in the QGIS task manager that lists the files below {title}.
        task = run_task(_("List {title}").format(title=document.title), build, planned)

    def _confirm(self, plan: download.Plan, clipped: int = 0) -> bool:
        files = len(plan.files)
        size = human_size(plan.known_bytes)
        lines = [
            ngettext("{n} file, at least {size}.", "{n} files, at least {size}.", files).format(
                n=number(files), size=size
            )
        ]
        if clipped:
            lines.append(
                count(
                    "{n} GeoParquet file downloads as GeoPackage, "
                    "with the features in the area only.",
                    "{n} GeoParquet files download as GeoPackage, "
                    "with the features in the area only.",
                    clipped,
                )
            )
        if plan.unknown_sizes:
            lines.append(
                count(
                    "{n} file does not declare a size.",
                    "{n} files do not declare a size.",
                    plan.unknown_sizes,
                )
            )
        if plan.failures:
            lines.append(
                count(
                    "{n} document could not be read and is left out.",
                    "{n} documents could not be read and are left out.",
                    len(plan.failures),
                )
            )
        if plan.truncated:
            lines.append(_("The listing stopped at its document limit."))
        if plan.outside:
            lines.append(
                count(
                    "{n} item outside the chosen area is left out.",
                    "{n} items outside the chosen area are left out.",
                    plan.outside,
                )
            )
        lines += ["", _("Download now?")]
        # Translators: the title of the dialog that confirms a download.
        answer = QMessageBox.question(self, _("Download"), "\n".join(lines))
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
        summary = ", ".join(
            (
                count("{n} downloaded", "{n} downloaded", len(report.downloaded)),
                count("{n} already present", "{n} already present", len(report.skipped)),
                count("{n} checksum verified", "{n} checksums verified", report.verified),
                count("{n} failed", "{n} failed", len(report.failed)),
            )
        )
        if report.cancelled:
            self._warn(_("Download cancelled: {summary}.").format(summary=summary))
        elif report.failed:
            self._warn(
                _("Download into {folder} finished with errors: {summary}.").format(
                    folder=folder, summary=summary
                )
            )
        else:
            self._info(
                _("Download into {folder} finished: {summary}.").format(
                    folder=folder, summary=summary
                )
            )

    def _show_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress.setFormat(f"{message}  (%v/%m)" if total else message)
        self.progress.show()
        self.task_bar.show()

    def _hide_progress(self) -> None:
        self.progress.hide()
        self.cancel.hide()
        self.task_bar.hide()

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


def _clip_of(scope: Scope) -> str | None:
    """Return the polygon that clips a mosaic for ``scope``, as WKT in EPSG:4326.

    A mosaic shows only the data in the area, so a map view clips to the
    view. Everything clips to nothing.
    """
    if isinstance(scope, Area):
        return scope.wkt
    if isinstance(scope, MapView):
        west, south, east, north = scope.bbox
        # A view across the antimeridian is two boxes, one on each side.
        spans = [(west, 180.0), (-180.0, east)] if west > east else [(west, east)]
        boxes = [
            f"(({w} {south},{e} {south},{e} {north},{w} {north},{w} {south}))" for w, e in spans
        ]
        return f"MULTIPOLYGON({','.join(boxes)})"
    return None


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
        lines.append(_("Layers: {layers}").format(layers=html.escape(", ".join(item.layers))))
    if item.asset is not None and item.asset.roles:
        lines.append(_("Roles: {roles}").format(roles=html.escape(", ".join(item.asset.roles))))
    if on_map:
        lines.append(_("A layer in the project already reads this file."))
    return "<br>".join(lines)


def _parquet_name(asset: Asset) -> str:
    """Return the layer name of a GeoParquet file."""
    return asset.title or asset.href.rsplit("/", 1)[-1].removesuffix(".parquet")


def clipped_path(href: str, root: str) -> str:
    """Return where the GeoPackage of a clipped GeoParquet file goes in the download folder.

    It keeps the file's place in the catalog layout, with a ``.gpkg`` suffix.
    """
    path = download.local_path(href, root)
    folder, _sep, name = path.rpartition("/")
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
        rows.append((_("Publisher"), html.escape(publisher)))
    name = license_name(document.license)
    licenses = hrefs("license")
    if len(licenses) == 1:
        rows.append((_("License"), anchor(licenses[0][0], name or link_label(*licenses[0]))))
    elif licenses:
        linked = ", ".join(anchor(href, link_label(href, title)) for href, title in licenses)
        rows.append((_("License"), f"{html.escape(name)}: {linked}" if name else linked))
    elif name:
        rows.append((_("License"), html.escape(name)))
    if size:
        rows.append((_("Size"), html.escape(human_size(size))))
    docs = hrefs("describedby")
    if docs:
        rows.append(
            (
                _("Documentation"),
                ", ".join(anchor(href, link_label(href, title)) for href, title in docs),
            )
        )
    sources = hrefs("via")
    if sources:
        # Translators: the label of the row that links to where the data comes from.
        label = _("Source")
        rows.append(
            (label, ", ".join(anchor(href, link_label(href, title)) for href, title in sources))
        )
    if browser:
        # Translators: the label of the row that links to more about the dataset.
        rows.append((_("More"), anchor(browser, _(BROWSER_LINK))))
    color = label_color.name()
    cells = "".join(
        f'<tr><td style="color: {color}; padding-right: 10px;">{html.escape(key)}</td>'  # i18n: skip
        f"<td>{value}</td></tr>"
        for key, value in rows
    )
    return f"<table>{cells}</table>"
