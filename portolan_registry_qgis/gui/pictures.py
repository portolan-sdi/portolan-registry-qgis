"""Pictures for the panel: remote logos and thumbnails, and extent maps.

Each catalog and node shows where its data is. ``WorldMap`` renders the
countries from the world map that QGIS includes, once, in the background.
``paint_extent`` crops that map around an extent and marks the extent on it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QByteArray, QObject, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QImage, QPainter, QPainterPath, QPalette, QPen, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer

from portolan_registry_qgis.core import footprint
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.qgis_io.network import fetch_bytes, run_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from portolan_registry_qgis.core.stac import Bbox

ACCENT = QColor("#4163cc")
WARNING = QColor("#b06e2a")
# One hue per format the panel loads, from the Portolan palette. Other
# formats take a neutral tone from the QGIS theme.
FORMAT_COLORS = {
    "pmtiles": QColor("#4163cc"),
    "parquet": QColor("#2f7d63"),
    "cog": QColor("#b06e2a"),
}
# The Portolan logomark in the brand blue, for a light theme, and in
# off-white, for a dark theme.
_IMAGES = Path(__file__).resolve().parent.parent / "images"
LOGOMARK_LIGHT = _IMAGES / "portolan-logomark-4163cc.svg"  # i18n: skip - a file name
LOGOMARK_DARK = _IMAGES / "portolan-logomark-fcfcfa.svg"  # i18n: skip - a file name
_WORLD_SIZE = QSize(2048, 1024)
_LARGEST_IMAGE = 720
_LARGEST_SVG = 192
_IMAGE_ACCEPT = b"image/avif, image/webp, image/png, image/svg+xml, image/*;q=0.8"


def mix(first: QColor, second: QColor, amount: float) -> QColor:
    """Return ``first`` moved ``amount`` of the way to ``second``."""
    return QColor.fromRgbF(
        first.redF() + (second.redF() - first.redF()) * amount,
        first.greenF() + (second.greenF() - first.greenF()) * amount,
        first.blueF() + (second.blueF() - first.blueF()) * amount,
    )


def muted(palette: QPalette, selected: bool = False) -> QColor:
    """Return the color for secondary text."""
    if selected:
        return mix(
            palette.color(QPalette.ColorRole.HighlightedText),
            palette.color(QPalette.ColorRole.Highlight),
            0.3,
        )
    return mix(palette.color(QPalette.ColorRole.Text), palette.color(QPalette.ColorRole.Base), 0.42)


def is_dark(palette: QPalette) -> bool:
    """Return whether the theme draws views on a dark background."""
    return palette.color(QPalette.ColorRole.Base).lightness() < 128


def logomark(palette: QPalette) -> Path:
    """Return the Portolan logomark file that shows on ``palette``."""
    return LOGOMARK_DARK if is_dark(palette) else LOGOMARK_LIGHT


@dataclass(frozen=True)
class Picture:
    """A decoded image.

    ``monochrome`` marks an SVG drawn in ``currentColor``, such as a Lucide
    icon. The panel tints it with the text color, so it shows on a dark theme.
    """

    image: QImage
    monochrome: bool


def decode(data: bytes) -> Picture | None:
    """Decode PNG, JPEG, WebP, GIF, or SVG bytes, or return None.

    Runs off the main thread. It draws only into a ``QImage``, which is safe
    there.
    """
    head = data[:1024].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:4096].lower():
        renderer = QSvgRenderer(QByteArray(data))
        if not renderer.isValid():
            return None
        size = renderer.defaultSize()
        if size.isEmpty():
            size = QSize(_LARGEST_SVG, _LARGEST_SVG)
        size = size.scaled(_LARGEST_SVG, _LARGEST_SVG, Qt.AspectRatioMode.KeepAspectRatio)
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        return Picture(image, b"currentcolor" in data.lower())
    image = QImage.fromData(data)
    if image.isNull():
        return None
    if max(image.width(), image.height()) > _LARGEST_IMAGE:
        image = image.scaled(
            _LARGEST_IMAGE,
            _LARGEST_IMAGE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return Picture(image, False)


def tinted(image: QImage, color: QColor) -> QImage:
    """Return ``image`` with every visible pixel painted ``color``."""
    result = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(result.rect(), color)
    painter.end()
    return result


def pixmap(picture: Picture, size: QSize, color: QColor, ratio: float = 1.0) -> QPixmap:
    """Scale ``picture`` to fit ``size`` for a screen with device ``ratio``."""
    image = picture.image.scaled(
        size * ratio, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    if picture.monochrome:
        image = tinted(image, color)
    result = QPixmap.fromImage(image)
    result.setDevicePixelRatio(ratio)
    return result


class ImageCache(QObject):
    """Fetch and decode remote images once each, off the main thread."""

    loaded = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pictures: dict[str, Picture | None] = {}
        self._waiting: dict[str, list[Callable[[Picture | None], None]]] = {}
        self._running: set[str] = set()

    def get(self, url: str | None) -> Picture | None:
        """Return the picture at ``url`` if it is ready, and start reading it if not.

        ``loaded`` fires when a read finishes.
        """
        if not url:
            return None
        if url not in self._pictures:
            self._load(url)
        return self._pictures.get(url)

    def request(self, url: str, callback: Callable[[Picture | None], None]) -> None:
        """Call ``callback`` with the picture, or None when it cannot be read."""
        if url in self._pictures:
            callback(self._pictures[url])
            return
        self._waiting.setdefault(url, []).append(callback)
        self._load(url)

    def _load(self, url: str) -> None:
        if url in self._running:
            return
        self._running.add(url)

        def read(_task: object) -> Picture | None:
            return decode(fetch_bytes(url, _IMAGE_ACCEPT))

        def done(result: object, _error: BaseException | None) -> None:
            self._running.discard(url)
            picture = result if isinstance(result, Picture) else None
            self._pictures[url] = picture
            for callback in self._waiting.pop(url, []):
                # The widget that asked may be gone by now.
                with contextlib.suppress(RuntimeError):
                    callback(picture)
            with contextlib.suppress(RuntimeError):
                self.loaded.emit(url)

        # Translators: A background task that reads the image at {url}.
        run_task(_("Read {url}").format(url=url), read, done, hidden=True)


class WorldMap(QObject):
    """The countries of the world as a plate carrée mask, rendered once."""

    ready = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._mask: QImage | None = None
        self._tints: dict[int, QImage] = {}
        self._job: Any = None
        self._layer: Any = None
        self._started = False

    @staticmethod
    def path() -> Path:
        """Return where QGIS keeps its world map."""
        data = Path(QgsApplication.pkgDataPath()) / "resources" / "data"
        return data / "world_map.gpkg"  # i18n: skip - a file name

    def image(self, color: QColor) -> QImage | None:
        """Return the land in ``color`` on a clear background, once it is rendered."""
        if self._mask is None:
            self._start()
            return None
        key = color.rgba()
        if key not in self._tints:
            self._tints[key] = tinted(self._mask, color)
        return self._tints[key]

    def _start(self) -> None:
        if self._started:
            return
        self._started = True
        path = self.path()
        if not path.is_file():
            return
        options = QgsVectorLayer.LayerOptions()
        options.loadDefaultStyle = False
        # The file belongs to the QGIS install, which a user cannot write to.
        options.forceReadOnly = True
        layer = QgsVectorLayer(f"{path}|layername=countries", "world", "ogr", options)
        if not layer.isValid():
            return
        symbol = QgsFillSymbol.createSimple({"color": "255,255,255,255", "outline_style": "no"})
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        settings = QgsMapSettings()
        settings.setLayers([layer])
        settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        settings.setOutputSize(_WORLD_SIZE)
        settings.setExtent(QgsRectangle(-180, -90, 180, 90))
        settings.setBackgroundColor(QColor(0, 0, 0, 0))
        settings.setFlag(QgsMapSettings.Flag.Antialiasing, True)
        job = QgsMapRendererParallelJob(settings)
        job.finished.connect(self._rendered)
        self._layer, self._job = layer, job
        job.start()

    def _rendered(self) -> None:
        if self._job is None:
            return
        self._mask = self._job.renderedImage()
        self._job = self._layer = None
        with contextlib.suppress(RuntimeError):
            self.ready.emit()

    def stop(self) -> None:
        """Cancel a render that is still running."""
        if self._job is not None:
            self._job.cancelWithoutBlocking()
            self._job = None


def paint_extent(
    painter: QPainter,
    rect: QRectF,
    bbox: Bbox | None,
    world: WorldMap,
    palette: QPalette,
    min_span: float = 30.0,
    radius: float = 3.0,
) -> None:
    """Draw the land around ``bbox`` in ``rect`` and mark ``bbox`` on it.

    An extent too small to see draws as a dot. An extent that fills the
    picture tints the land instead. Without a world map, a graticule stands
    in for the land.
    """
    if rect.width() < 2 or rect.height() < 2:
        return
    base = palette.color(QPalette.ColorRole.Base)
    text = palette.color(QPalette.ColorRole.Text)
    water = mix(base, text, 0.05)
    land = mix(base, text, 0.2 if is_dark(palette) else 0.16)
    view = footprint.window(bbox, rect.width() / rect.height(), min_span)
    everywhere = bbox is not None and footprint.covers(bbox, view)
    if everywhere:
        land = mix(land, ACCENT, 0.5)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    painter.setClipPath(clip)
    painter.fillRect(rect, water)
    mask = world.image(land)
    if mask is not None:
        width, height = mask.width(), mask.height()
        source = QRectF(
            (view[0] + 180) / 360 * width,
            (90 - view[3]) / 180 * height,
            (view[2] - view[0]) / 360 * width,
            (view[3] - view[1]) / 180 * height,
        )
        painter.drawImage(rect, mask, source)
    else:
        _graticule(painter, rect, view, land)
    if bbox is not None and not everywhere:
        _mark(painter, rect, bbox, view, base)
    painter.setClipping(False)
    painter.setPen(QPen(mix(base, text, 0.22), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    painter.restore()


def _graticule(painter: QPainter, rect: QRectF, view: Bbox, color: QColor) -> None:
    painter.setPen(QPen(color, 1))
    sx = rect.width() / (view[2] - view[0])
    sy = rect.height() / (view[3] - view[1])
    for lon in range(-180, 181, 15):
        x = rect.x() + (lon - view[0]) * sx
        painter.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
    for lat in range(-90, 91, 15):
        y = rect.y() + (view[3] - lat) * sy
        painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))


def _mark(painter: QPainter, rect: QRectF, bbox: Bbox, view: Bbox, base: QColor) -> None:
    for x, y, w, h in footprint.rects(bbox, view, rect.width(), rect.height()):
        box = QRectF(rect.x() + x, rect.y() + y, w, h)
        if w < 5 and h < 5:
            painter.setPen(QPen(base, 1.5))
            painter.setBrush(ACCENT)
            painter.drawEllipse(box.center(), 3.5, 3.5)
            continue
        fill = QColor(ACCENT)
        fill.setAlpha(70)
        painter.setPen(QPen(ACCENT, 1.5))
        painter.setBrush(fill)
        painter.drawRect(box)
