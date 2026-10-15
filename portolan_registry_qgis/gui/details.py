"""The right pane: the chosen node in collapsible sections.

The window enables the buttons. ``state_changed`` asks it to decide again.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from qgis.core import QgsProject
from qgis.gui import QgsCollapsibleGroupBox
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAction,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.facts import document_extent, document_size, facts_html
from portolan_registry_qgis.core.format import format_label, human_size
from portolan_registry_qgis.core.i18n import _, count, number
from portolan_registry_qgis.core.loadable import (
    Targets,
    button_state,
    button_tip,
    files_hint,
    has_parquet,
    is_on_map,
    is_table,
    item_file,
    loadable_tip,
    only_items,
    pick_targets,
)
from portolan_registry_qgis.core.scope import Everything, Scope
from portolan_registry_qgis.core.stac import Asset, Document, Loadable, Node
from portolan_registry_qgis.core.style import archive_urls
from portolan_registry_qgis.gui import widgets
from portolan_registry_qgis.gui.pictures import ACCENT, mix, muted
from portolan_registry_qgis.gui.scope import ScopePicker
from portolan_registry_qgis.gui.widgets import (
    BADGE_ROLE,
    ON_MAP_ROLE,
    ROLE,
    AssetDelegate,
    HeroView,
    More,
    open_label,
    theme_icon,
)
from portolan_registry_qgis.qgis_io.canvas import on_map

if TYPE_CHECKING:
    from collections.abc import Callable

    from portolan_registry_qgis.core.registry import CatalogEntry
    from portolan_registry_qgis.gui.pictures import ImageCache, Picture, WorldMap
    from portolan_registry_qgis.qgis_io.documents import DocumentCache

# The sections keep their open state in the QGIS settings under this group.
SETTINGS_GROUP = "PortolanRegistry"


def section(
    title: str, name: str, *, collapsed: bool = False
) -> tuple[QgsCollapsibleGroupBox, QVBoxLayout]:
    """Return a collapsible section and the layout for its content.

    The section keeps its open state in the QGIS settings under ``name``.
    A collapse hides direct children, so the content goes in one inner widget.
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


def accent(button: QPushButton) -> None:
    """Paint the primary action in the Portolan blue."""
    hover = mix(ACCENT, QColor("#16170f"), 0.18).name()
    color = ACCENT.name()
    button.setStyleSheet(
        f"QPushButton {{ background: {color}; color: #fcfcfa; border: 1px solid {hover};"  # i18n: skip
        " border-radius: 3px; padding: 4px 12px; font-weight: bold; }"
        f"QPushButton:hover {{ background: {hover}; }}"  # i18n: skip
        "QPushButton:disabled { background: palette(midlight); color: palette(mid);"
        " border-color: palette(mid); font-weight: normal; }"
    )


def _button(text: str, icon: str) -> QPushButton:
    button = QPushButton(text)
    button.setIcon(theme_icon(icon))
    return button


def _label(text_format: Qt.TextFormat) -> QLabel:
    """Return a label that wraps its text and opens its links in the browser."""
    label = QLabel()
    label.setWordWrap(True)
    label.setTextFormat(text_format)
    label.setOpenExternalLinks(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    return label


class DetailsPanel(QScrollArea):
    """Show one document: its picture, its actions, its files, and its facts."""

    # Emits when the selected files, the area, or the file rows change.
    state_changed = pyqtSignal()

    def __init__(
        self,
        iface: Any,
        images: ImageCache,
        world: WorldMap,
        documents: DocumentCache,
        current: Callable[[], Document | None],
    ):
        super().__init__()
        self._images = images
        self._world = world
        self._documents = documents
        # The window keeps the open document. A late callback compares its
        # document with this one, and does nothing when they differ.
        self._current = current
        # The files that "View on map" and "Load full data" add.
        self._targets = Targets(None, None)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.empty = QLabel(_("Choose a publisher on the left to see its data here."))
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color: {self._muted()};")
        layout.addWidget(self.empty, 1)
        layout.addWidget(self._title_row())
        layout.addWidget(self._about_section())
        layout.addWidget(self._add_section(iface))
        layout.addWidget(self._files_section())
        layout.addWidget(self._details_section())
        layout.addStretch(1)
        self.hide_actions()
        self.show_sections(visible=False)
        self.setWidget(body)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._connect()

    # ---------- layout ----------

    def _title_row(self) -> QWidget:
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
        self.zoom.setIcon(theme_icon("mActionZoomToLayer"))
        self.zoom.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.zoom.setAutoRaise(True)
        self.zoom.setToolTip(_("Zoom the map to the extent of this dataset"))
        title_layout.addWidget(self.zoom, 0, Qt.AlignmentFlag.AlignTop)
        # Hidden until the window can build the browser link of a node.
        self.open_browser = QToolButton()
        self.open_browser.setText(open_label())
        self.open_browser.setAutoRaise(True)
        self.open_browser.hide()
        title_layout.addWidget(self.open_browser, 0, Qt.AlignmentFlag.AlignTop)
        return self.title_row

    def _about_section(self) -> QgsCollapsibleGroupBox:
        # Translators: the title of the section that describes the dataset.
        self.about_box, about = section(_("About"), "about")
        self.hero = HeroView(self._world)
        about.addWidget(self.hero)
        self.description = _label(Qt.TextFormat.MarkdownText)
        about.addWidget(self.description)
        return self.about_box

    def _scope_row(self, iface: Any) -> QWidget:
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
        self.scope_picker = ScopePicker(iface)
        scope_layout.addWidget(self.scope_picker)
        self.scope_hint = QLabel()
        self.scope_hint.setWordWrap(True)
        scope_layout.addWidget(self.scope_hint)
        return self.scope_row

    def _add_section(self, iface: Any) -> QgsCollapsibleGroupBox:
        # Every action in this section reads only the data in the area, so
        # the area comes first.
        self.add_box, add = section(_("Add to map"), "add")
        add.addWidget(self._scope_row(iface))
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.view_button = _button(_("View on map"), "mActionAddLayer")
        accent(self.view_button)
        self.load_button = _button(_("Load full data"), "mActionAddOgrLayer")
        self.mosaic_button = _button(_("Add as one layer"), "mActionAddRasterLayer")
        self.mosaic_button.setToolTip(
            _("Add the COG files in the area as one raster layer, clipped to the area")
        )
        accent(self.mosaic_button)
        # Translators: a button that asks where to download the files.
        download_label = _("Download…")
        self.download_all = QAction(theme_icon("mActionFileSave"), download_label, self)
        self.download_area = QToolButton()
        self.download_area.setDefaultAction(self.download_all)
        self.download_area.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for widget in (self.view_button, self.load_button, self.mosaic_button):
            actions.addWidget(widget)
        actions.addWidget(self.download_area)
        actions.addStretch(1)
        add.addLayout(actions)
        self.tile_styles = QLabel()
        self.tile_styles.setWordWrap(True)
        add.addWidget(self.tile_styles)
        return self.add_box

    def _files_section(self) -> QgsCollapsibleGroupBox:
        # The buttons in "Add to map" cover most needs. The list of every file
        # starts closed. A new setting name lets the closed start reach users
        # who opened the section before.
        self.files_box, files = section(_("All files"), "all_files", collapsed=True)
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
        self.add = _button(_("Add selected"), "mActionAddLayer")
        self.add.setToolTip(_("Add the selected files to the map, whole"))
        self.download_selected = QAction(
            theme_icon("mActionFileSave"),
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
        return self.files_box

    def _details_section(self) -> QgsCollapsibleGroupBox:
        self.details_box, details = section(_("Details"), "details")
        self.facts = _label(Qt.TextFormat.RichText)
        details.addWidget(self.facts)
        return self.details_box

    def _connect(self) -> None:
        self.assets.itemSelectionChanged.connect(self.state_changed)
        self.assets.itemClicked.connect(self._asset_clicked)
        self.scope_picker.changed.connect(self.state_changed)
        project = QgsProject.instance()
        project.layersAdded.connect(self.mark_on_map)
        project.layersRemoved.connect(self.mark_on_map)

    def release(self) -> None:
        """Stop following the project and the map. Call before the panel is deleted."""
        project = QgsProject.instance()
        with contextlib.suppress(TypeError):
            project.layersAdded.disconnect(self.mark_on_map)
        with contextlib.suppress(TypeError):
            project.layersRemoved.disconnect(self.mark_on_map)
        self.scope_picker.release()

    def _muted(self) -> str:
        return muted(self.palette()).name()

    def _actions(self) -> tuple[QWidget, ...]:
        """Return the parts of "Add to map" that show only when they fit the document."""
        return (
            self.view_button,
            self.load_button,
            self.mosaic_button,
            self.tile_styles,
            self.scope_row,
        )

    # ---------- what shows ----------

    def show_sections(self, *, visible: bool) -> None:
        """Show the title and sections, or the hint when no node is chosen."""
        self.empty.setVisible(not visible)
        for widget in (self.title_row, self.about_box, self.files_box, self.details_box):
            widget.setVisible(visible)
        self.add_box.setVisible(visible and not all(part.isHidden() for part in self._actions()))

    def hide_actions(self) -> None:
        """Hide the primary buttons, the style note, and the scope picker."""
        for widget in self._actions():
            widget.hide()

    def show_browser_link(self, link: str | None) -> None:
        """Show the browser button for ``link``, or hide it when ``link`` is None."""
        self.open_browser.setVisible(link is not None)
        tip = _("See every file and all metadata of this dataset")
        self.open_browser.setToolTip(f"{tip}\n{link}")

    def show_reading(self, title: str, link: str | None) -> None:
        """Show ``title`` while its document is read. Call when the window has no document."""
        self._targets = Targets(None, None)
        self.show_browser_link(link)
        self.title.setText(title)
        self.description.setText(_("Reading…"))
        self.facts.clear()
        self.assets.clear()
        self.fit_assets()
        self.hero.show_extent(None)
        self.hero.hide()
        self.show_sections(visible=True)

    def show_failure(self, title: str, href: str, error: BaseException | None) -> None:
        """Say that the document at ``href`` could not be read."""
        self.title.setText(title)
        self.description.setTextFormat(Qt.TextFormat.PlainText)
        self.description.setText(_("Could not read {url}: {error}").format(url=href, error=error))
        self.description.setTextFormat(Qt.TextFormat.MarkdownText)

    def _show_about(self, document: Document, catalog: CatalogEntry | None) -> None:
        bbox = document_extent(document, catalog)
        self.hero.show_extent(bbox)
        self.hero.setVisible(bbox is not None)
        thumb = document.thumbnail
        if thumb is not None:

            def show(picture: Picture | None) -> None:
                if self._current() is document and picture is not None:
                    self.hero.set_picture(picture)
                    self.hero.show()

            self._images.request(thumb.href, show)
        self.title.setText(document.title)
        self.description.setText(document.description)
        self.description.setVisible(bool(document.description))

    def show_document(
        self, document: Document, catalog: CatalogEntry | None, link: str | None
    ) -> None:
        """Show ``document``, the window's open document, with its files and actions.

        ``catalog`` is the open publisher, and ``link`` is the browser page
        of the document.
        """
        self._show_about(document, catalog)
        publisher = catalog.title if catalog is not None else None
        size = document_size(document, catalog)
        self.facts.setText(facts_html(document, publisher, size, self._muted(), link))
        self.show_browser_link(link)
        self.download_all.setToolTip(
            _("Download the files of {title} in the area").format(title=document.title)
        )
        # Only the files QGIS can open show, and an archive that is a link
        # and an asset shows once.
        loadable = document.loadable()
        self.assets.clear()
        for item in loadable:
            row = QTreeWidgetItem([item.title])
            row.setData(0, ROLE, item)
            asset = item.asset
            if asset is None:
                row.setData(0, BADGE_ROLE, ("PMTiles", "pmtiles", ""))
            else:
                badge = format_label(asset.format, asset.type, asset.href, tabular=asset.tabular)
                row.setData(0, BADGE_ROLE, (badge, asset.format, human_size(asset.size)))
            self.assets.addTopLevelItem(row)
        if only_items(document):
            # The rows let the user pick single files. "Add as one layer" in
            # "Add to map" covers every file in the area.
            self._add_item_rows(document, document.children)
        self.mark_on_map()
        self.fit_assets()
        self._show_actions(document, loadable)
        self.show_sections(visible=True)

    def _show_actions(self, document: Document, loadable: tuple[Loadable, ...]) -> None:
        """Show the primary buttons, the style note, and the scope picker that fit the document."""
        self._targets = pick_targets(loadable, duckdb_ok=parquet_query.duckdb_status()[0])
        load = self._targets.load
        # A table has no geometry, so it loads whole and says so.
        table = load is not None and is_table(load)
        self.load_button.setText(_("Load table") if table else _("Load full data"))
        for button, target in ((self.view_button, self._targets.view), (self.load_button, load)):
            button.setVisible(target is not None)
            if target is not None:
                button.setToolTip(
                    button_tip(target, view=button is self.view_button, document=document)
                )
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
        self.tile_styles.setStyleSheet(f"color: {self._muted()};")
        self.tile_styles.setVisible(bool(styles and archive_urls(document)))
        # The scope limits GeoParquet reads, mosaics, and the walk of a download.
        self.scope_row.setVisible(has_parquet(loadable) or bool(document.children))

    # ---------- item files ----------

    def _add_item_rows(self, document: Document, nodes: tuple[Node, ...]) -> None:
        """Add one page of item rows, and a row that lists the rest.

        Each row shows the data file of its item after the item is read.
        """
        page = widgets.NODE_PAGE
        for node in nodes[:page]:
            row = QTreeWidgetItem([node.title])
            row.setData(0, ROLE, node)
            row.setToolTip(0, _("Reading…"))
            self.assets.addTopLevelItem(row)
            self._documents.open(node.href, self._item_read(document, row), quiet=True)
        rest = nodes[page:]
        if rest:
            more = More(rest)
            row = QTreeWidgetItem([more.label(page, "file")])
            row.setData(0, ROLE, more)
            row.setForeground(0, QBrush(ACCENT))
            # The row is a button, so a selection skips it.
            row.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.assets.addTopLevelItem(row)
        self.fit_assets()

    def _item_read(
        self, document: Document, row: QTreeWidgetItem
    ) -> Callable[[object, BaseException | None], None]:
        """Return a callback that shows the data file of a read item in ``row``."""

        def opened(item: object, _error: BaseException | None) -> None:
            if self._current() is not document:
                return
            read = item if isinstance(item, Document) else None
            found = item_file(read) if read is not None else None
            if found is None or found.asset is None:
                # The item has no file to add, or it cannot be read.
                index = self.assets.indexOfTopLevelItem(row)
                if index >= 0:
                    self.assets.takeTopLevelItem(index)
            else:
                asset = found.asset
                row.setData(0, ROLE, found)
                badge = format_label(asset.format, asset.type, asset.href, tabular=asset.tabular)
                row.setData(0, BADGE_ROLE, (badge, asset.format, human_size(asset.size)))
            self.mark_on_map()
            self.fit_assets()
            self.state_changed.emit()

        return opened

    def _asset_clicked(self, row: QTreeWidgetItem | None, _column: int = 0) -> None:
        """List the next page of item files when the user clicks the "Show more" row."""
        document = self._current()
        if row is None or document is None or not isinstance(more := row.data(0, ROLE), More):
            return
        self.assets.takeTopLevelItem(self.assets.indexOfTopLevelItem(row))
        self._add_item_rows(document, more.rest)

    def mark_on_map(self, *_args: object) -> None:
        """Mark the file rows that a layer in the project already reads."""
        urls, sources = on_map()
        for i in range(self.assets.topLevelItemCount()):
            row = self.assets.topLevelItem(i)
            item = row.data(0, ROLE)
            if not isinstance(item, Loadable):
                continue
            mapped = is_on_map(item, urls, sources)
            row.setData(0, ON_MAP_ROLE, mapped)
            row.setToolTip(0, loadable_tip(item, on_map=mapped))

    def fit_assets(self) -> None:
        """Size the file list to its rows, so the details panel scrolls instead."""
        rows = self.assets.topLevelItemCount()
        self.assets.setVisible(rows > 0)
        self.files_box.setTitle(
            _("All files ({count})").format(count=number(rows)) if rows else _("All files")
        )
        if rows:
            height = sum(self.assets.sizeHintForRow(i) for i in range(rows))
            self.assets.setFixedHeight(height + 2 * self.assets.frameWidth() + 2)
        document = self._current()
        text = files_hint(document, rows)
        if text:
            self.no_assets.setText(text)
            self.no_assets.setStyleSheet(f"color: {self._muted()};")
            self.no_assets.show()
        else:
            self.no_assets.hide()
        if document is None:
            self.hide_actions()

    # ---------- choices ----------

    @property
    def targets(self) -> Targets:
        """Return the files that "View on map" and "Load full data" add."""
        return self._targets

    def selected(self) -> tuple[list[str], list[Asset]]:
        """Return the selected PMTiles URLs and the selected assets."""
        tiles: list[str] = []
        assets: list[Asset] = []
        for row in self.assets.selectedItems():
            value = row.data(0, ROLE)
            if not isinstance(value, Loadable):
                continue
            if value.format == "pmtiles":
                tiles.append(value.href)
            elif value.asset is not None:
                assets.append(value.asset)
        return tiles, assets

    def scope(self) -> Scope:
        """Return the chosen scope, or Everything when the picker is hidden.

        Raises:
            ScopeError: The choice is not complete.
        """
        if self.scope_row.isHidden():
            return Everything()
        return self.scope_picker.scope()

    def sync_buttons(self, document: Document | None, *, zoomable: bool, busy: bool) -> None:
        """Enable the buttons that can act on ``document`` now.

        ``zoomable`` says whether the document has an extent, and ``busy``
        whether a long task runs.
        """
        tiles, assets = self.selected()
        self.zoom.setEnabled(zoomable)
        problem = None if self.scope_row.isHidden() else self.scope_picker.problem()
        self.scope_hint.setText(problem or "")
        self.scope_hint.setStyleSheet(f"color: {self._muted()};")
        self.scope_hint.setVisible(problem is not None)
        state = button_state(
            document,
            self._targets,
            selected=bool(tiles or assets),
            ready=problem is None,
            busy=busy,
        )
        self.add.setEnabled(state.add)
        self.view_button.setEnabled(state.view)
        self.load_button.setEnabled(state.load)
        self.mosaic_button.setEnabled(state.mosaic)
        self.download_selected.setEnabled(state.download_selected)
        self.download_all.setEnabled(state.download_all)
