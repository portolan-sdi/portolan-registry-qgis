"""Item delegates and small widgets that draw the panel's pictures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from qgis.PyQt.QtCore import QRect, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from qgis.PyQt.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from portolan_registry_qgis.core.format import catalog_summary, count
from portolan_registry_qgis.core.registry import CatalogEntry
from portolan_registry_qgis.gui.pictures import (
    ACCENT,
    FORMAT_COLORS,
    WARNING,
    ImageCache,
    Picture,
    WorldMap,
    is_dark,
    mix,
    muted,
    paint_extent,
    pixmap,
)

if TYPE_CHECKING:
    from qgis.PyQt.QtCore import QModelIndex

    from portolan_registry_qgis.core.stac import Bbox

ROLE = Qt.ItemDataRole.UserRole
# Asset rows keep (badge text, format, size text) here, for AssetDelegate.
BADGE_ROLE = Qt.ItemDataRole.UserRole + 1

_PAD = 8


@dataclass(frozen=True)
class More:
    """The rows a list holds back. Its row shows the next page when clicked."""

    rest: tuple[Any, ...]

    def label(self, page: int, noun: str = "") -> str:
        """Return the row's text. A ``noun`` names what the rows hold."""
        shown = min(page, len(self.rest))
        total = count(len(self.rest), noun) if noun else f"{len(self.rest):,}"
        return f"Show {shown:,} more of {total}"


_TILE = QSize(66, 44)
_LOGO = QSize(60, 30)


def _selected(option: QStyleOptionViewItem) -> bool:
    return bool(option.state & QStyle.StateFlag.State_Selected)


def _panel(painter: QPainter, option: QStyleOptionViewItem) -> None:
    """Draw the row background the style uses for hover and selection."""
    style = option.widget.style() if option.widget is not None else QApplication.style()
    style.drawPrimitive(
        QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget
    )


def _text_color(option: QStyleOptionViewItem) -> QColor:
    role = QPalette.ColorRole.HighlightedText if _selected(option) else QPalette.ColorRole.Text
    return option.palette.color(role)


def _logo(
    painter: QPainter, area: QRect, picture: Picture, palette: QPalette, text: QColor
) -> None:
    """Draw a logo right-aligned in ``area``, on a light chip in a dark theme."""
    ratio = painter.device().devicePixelRatioF() if painter.device() else 1.0
    image = pixmap(picture, area.size(), text, ratio)
    size = image.size() / ratio
    target = QRect(
        area.right() - size.width() + 1,
        area.center().y() - size.height() // 2,
        size.width(),
        size.height(),
    )
    if is_dark(palette) and not picture.monochrome:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#fcfcfa"))
        painter.drawRoundedRect(QRectF(target).adjusted(-4, -3, 4, 3), 3, 3)
    painter.drawPixmap(target, image)


def _wrap(metrics: QFontMetrics, text: str, width: int) -> list[str]:
    """Break ``text`` into at most two lines of ``width``, eliding the second."""
    words = text.split()
    first = ""
    while words and metrics.horizontalAdvance(f"{first} {words[0]}".strip()) <= width:
        first = f"{first} {words.pop(0)}".strip()
    if not first:
        return [metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)]
    if not words:
        return [first]
    return [first, metrics.elidedText(" ".join(words), Qt.TextElideMode.ElideRight, width)]


class CatalogDelegate(QStyledItemDelegate):
    """A registry row: the extent map, the title, one line of facts, and the logo."""

    def __init__(self, view: Any, images: ImageCache, world: WorldMap):
        super().__init__(view)
        self._view = view
        self._images = images
        self._world = world
        images.loaded.connect(lambda _url: view.viewport().update())
        world.ready.connect(view.viewport().update)

    @staticmethod
    def _bold(option: QStyleOptionViewItem) -> QFont:
        font = QFont(option.font)
        font.setBold(True)
        return font

    @staticmethod
    def _text_span(entry: CatalogEntry, left: int, right: int) -> tuple[int, int]:
        """Return the left edge and width of the text, beside the tile and the logo."""
        start = left + _PAD + _TILE.width() + _PAD + 4
        end = right - _PAD - (_LOGO.width() + _PAD * 2 - 4 if entry.logo_url else 0)
        return start, max(0, end - start)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Return the row size, with room for a title on two lines."""
        line = option.fontMetrics.height()
        entry = index.data(ROLE)
        if isinstance(entry, More):
            return QSize(0, line * 3)
        if not isinstance(entry, CatalogEntry):
            return QSize(0, line * 4)
        # Measure as if the scrollbar shows. A width that changes when it
        # appears would leave rows at a stale height.
        scrollbar = self._view.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        width = self._view.width() - 2 * self._view.frameWidth() - scrollbar
        _, text_width = self._text_span(entry, 0, width)
        bold = QFontMetrics(self._bold(option))
        lines = len(_wrap(bold, entry.title, text_width))
        text_height = bold.height() * lines + 2 + line
        return QSize(0, max(_TILE.height(), text_height) + 2 * _PAD + 1)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Draw the row."""
        entry = index.data(ROLE)
        rect = option.rect
        palette = option.palette
        if not isinstance(entry, CatalogEntry):
            painter.save()
            if isinstance(entry, More):
                _panel(painter, option)
                painter.setPen(ACCENT if not _selected(option) else _text_color(option))
            else:
                painter.setPen(muted(palette))
            painter.drawText(
                rect.adjusted(_PAD * 2, 0, -_PAD * 2, 0),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
            )
            painter.restore()
            return
        painter.save()
        _panel(painter, option)
        selected = _selected(option)
        text = _text_color(option)
        tile = QRectF(
            rect.left() + _PAD,
            rect.center().y() - _TILE.height() / 2 + 0.5,
            _TILE.width(),
            _TILE.height(),
        )
        paint_extent(painter, tile, entry.bbox, self._world, palette)

        picture = self._images.get(entry.logo_url)
        if picture is not None:
            area = QRect(
                rect.right() - _PAD - _LOGO.width() + 1,
                rect.center().y() - _LOGO.height() // 2,
                _LOGO.width(),
                _LOGO.height(),
            )
            _logo(painter, area, picture, palette, text)

        left, width = self._text_span(entry, rect.left(), rect.right())
        bold = self._bold(option)
        bold_metrics = QFontMetrics(bold)
        metrics = option.fontMetrics
        title = _wrap(bold_metrics, entry.title, width)
        height = bold_metrics.height() * len(title) + 2 + metrics.height()
        top = rect.center().y() - height // 2
        painter.setFont(bold)
        painter.setPen(text)
        for number, words in enumerate(title):
            painter.drawText(
                QRect(left, top + number * bold_metrics.height(), width, bold_metrics.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                words,
            )
        painter.setFont(option.font)
        second = QRect(left, top + bold_metrics.height() * len(title) + 2, width, metrics.height())
        if entry.status != "valid":
            status = entry.status.capitalize()
            painter.setPen(text if selected else WARNING)
            painter.drawText(
                second, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, status
            )
            shift = metrics.horizontalAdvance(status + "  ")
            second.adjust(shift, 0, 0, 0)
        painter.setPen(muted(palette, selected))
        summary = catalog_summary(entry.collection_count, entry.total_size_bytes, entry.licenses)
        painter.drawText(
            second,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(summary, Qt.TextElideMode.ElideRight, second.width()),
        )
        painter.setPen(
            mix(palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text), 0.1)
        )
        painter.drawLine(rect.left() + _PAD, rect.bottom(), rect.right() - _PAD, rect.bottom())
        painter.restore()


class AssetDelegate(QStyledItemDelegate):
    """An asset row: a format badge, the title, and the size."""

    def __init__(self, view: Any):
        super().__init__(view)
        self._badge_width = 0

    @staticmethod
    def _small(option: QStyleOptionViewItem) -> QFont:
        font = QFont(option.font)
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.85))
        font.setBold(True)
        return font

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Return the row size."""
        del index
        return QSize(0, option.fontMetrics.height() + 14)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Draw the row."""
        badge = index.data(BADGE_ROLE)
        if not badge:
            super().paint(painter, option, index)
            return
        label, format_key, size = badge
        painter.save()
        _panel(painter, option)
        palette = option.palette
        rect = option.rect
        small = self._small(option)
        small_metrics = QFontMetrics(small)
        if not self._badge_width:
            self._badge_width = small_metrics.horizontalAdvance("GeoParquet") + 12
        color = FORMAT_COLORS.get(format_key)
        base = palette.color(QPalette.ColorRole.Base)
        text = palette.color(QPalette.ColorRole.Text)
        fill = color if color is not None else mix(base, text, 0.14)
        ink = QColor("#fcfcfa") if color is not None else mix(base, text, 0.75)
        height = small_metrics.height() + 4
        box = QRectF(
            rect.left() + 6, rect.center().y() - height / 2 + 0.5, self._badge_width, height
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(box, 3, 3)
        painter.setFont(small)
        painter.setPen(ink)
        painter.drawText(
            box,
            Qt.AlignmentFlag.AlignCenter,
            small_metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(box.width()) - 6),
        )

        metrics = option.fontMetrics
        painter.setFont(option.font)
        right = rect.right() - 6
        if size:
            size_width = metrics.horizontalAdvance(size)
            painter.setPen(muted(palette, _selected(option)))
            painter.drawText(
                QRect(right - size_width, rect.top(), size_width, rect.height()),
                Qt.AlignmentFlag.AlignVCenter,
                size,
            )
            right -= size_width + 10
        left = int(box.right()) + 10
        painter.setPen(_text_color(option))
        title = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.drawText(
            QRect(left, rect.top(), max(0, right - left), rect.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(title, Qt.TextElideMode.ElideRight, max(0, right - left)),
        )
        painter.restore()


class RoomyDelegate(QStyledItemDelegate):
    """The default row, with more height, for the catalog tree."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Return the row size."""
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(hint.height(), option.fontMetrics.height()) + 8)


class ExtentTile(QWidget):
    """A fixed-size extent map."""

    def __init__(self, world: WorldMap, size: QSize, parent: QWidget | None = None):
        super().__init__(parent)
        self._world = world
        self._bbox: Bbox | None = None
        self.setFixedSize(size)
        world.ready.connect(self.update)

    def set_bbox(self, bbox: Bbox | None) -> None:
        """Show ``bbox``."""
        self._bbox = bbox
        self.update()

    def paintEvent(self, _event: object) -> None:
        """Draw the picture."""
        painter = QPainter(self)
        paint_extent(painter, QRectF(self.rect()), self._bbox, self._world, self.palette())
        painter.end()


class HeroView(QWidget):
    """The chosen node's thumbnail, or a map of its extent when it has none.

    The picture and the map do not show together. ``painted_extent`` tells
    whether the last paint drew the map.
    """

    def __init__(self, world: WorldMap, parent: QWidget | None = None):
        super().__init__(parent)
        self._world = world
        self._bbox: Bbox | None = None
        self._picture: Picture | None = None
        self.painted_extent = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        world.ready.connect(self.update)

    def show_extent(self, bbox: Bbox | None, picture: Picture | None = None) -> None:
        """Show ``bbox``, or ``picture`` instead when there is one."""
        self._bbox = bbox
        self._picture = picture
        self.update()

    def set_picture(self, picture: Picture | None) -> None:
        """Show ``picture`` instead of the extent."""
        self._picture = picture
        self.update()

    @property
    def has_picture(self) -> bool:
        """Whether a thumbnail shows."""
        return self._picture is not None

    def hasHeightForWidth(self) -> bool:
        """Keep the picture at a fixed ratio to its width."""
        return True

    def heightForWidth(self, width: int) -> int:
        """Return the height for ``width``."""
        return int(min(220, max(110, width * 0.5)))

    def sizeHint(self) -> QSize:
        """Return the preferred size."""
        return QSize(320, self.heightForWidth(320))

    def paintEvent(self, _event: object) -> None:
        """Draw the picture."""
        painter = QPainter(self)
        rect = QRectF(self.rect())
        palette = self.palette()
        self.painted_extent = self._picture is None
        if self._picture is None:
            paint_extent(painter, rect, self._bbox, self._world, palette, min_span=40.0, radius=4.0)
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        base = palette.color(QPalette.ColorRole.Base)
        text = palette.color(QPalette.ColorRole.Text)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mix(base, text, 0.05))
        painter.drawRoundedRect(rect, 4, 4)
        ratio = self.devicePixelRatioF()
        image = pixmap(self._picture, rect.size().toSize(), text, ratio)
        size = image.size() / ratio
        painter.drawPixmap(
            int(rect.center().x() - size.width() / 2),
            int(rect.center().y() - size.height() / 2),
            image,
        )
        painter.end()


class LogoView(QWidget):
    """A logo at a fixed size, on a light chip in a dark theme."""

    def __init__(self, size: QSize, parent: QWidget | None = None):
        super().__init__(parent)
        self._picture: Picture | None = None
        self.setFixedSize(size)

    def set_picture(self, picture: Picture) -> None:
        """Show ``picture``."""
        self._picture = picture
        self.update()

    def paintEvent(self, _event: object) -> None:
        """Draw the logo."""
        if self._picture is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        area = self.rect().adjusted(4, 3, -4, -3)
        _logo(painter, area, self._picture, palette, palette.color(QPalette.ColorRole.Text))
        painter.end()


class CatalogHeader(QWidget):
    """The open publisher: its extent, title, facts, and logo. A click shows its details."""

    clicked = pyqtSignal()

    def __init__(self, world: WorldMap, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Show the publisher's details")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(_PAD, _PAD, _PAD, _PAD)
        layout.setSpacing(_PAD + 4)
        self.extent = ExtentTile(world, QSize(78, 52))
        layout.addWidget(self.extent)
        words = QVBoxLayout()
        words.setSpacing(2)
        self.title = QLabel()
        self.title.setWordWrap(True)
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.2)
        self.title.setFont(font)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        words.addStretch(1)
        words.addWidget(self.title)
        words.addWidget(self.summary)
        words.addStretch(1)
        layout.addLayout(words, 1)
        self.logo = LogoView(QSize(84, 40))
        self.logo.hide()
        layout.addWidget(self.logo)

    def show_entry(self, entry: CatalogEntry) -> None:
        """Show the registry's facts about ``entry``."""
        self.extent.set_bbox(entry.bbox)
        self.title.setText(entry.title)
        summary = catalog_summary(entry.collection_count, entry.total_size_bytes, entry.licenses)
        self.summary.setText(summary)
        self.summary.setVisible(bool(summary))
        palette = self.palette()
        color = muted(palette)
        self.summary.setStyleSheet(f"color: {color.name()};")
        self.logo.hide()

    def set_logo(self, picture: Picture | None) -> None:
        """Show ``picture`` as the catalog's logo."""
        if picture is not None:
            self.logo.set_picture(picture)
            self.logo.show()

    def mouseReleaseEvent(self, event: Any) -> None:
        """Emit ``clicked`` for a left click inside the header."""
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)
