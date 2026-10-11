"""The scope picker: everything, the map view, a drawn area, or a layer."""

from __future__ import annotations

import contextlib
from typing import Any

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMapLayer,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapLayerComboBox, QgsMapTool, QgsRubberBand
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QToolButton, QVBoxLayout, QWidget

from portolan_registry_qgis.core.scope import Area, Everything, MapView, Scope

EVERYTHING = "everything"
MAP_VIEW = "view"
DRAW = "draw"
FROM_LAYER = "layer"
CHOICES = (
    (EVERYTHING, "Everything"),
    (MAP_VIEW, "Map view"),
    (DRAW, "Draw an area…"),
    (FROM_LAYER, "From a layer…"),
)
WGS84 = "EPSG:4326"
_WORLD = (-180.0, -90.0, 180.0, 90.0)
_STROKE = QColor(65, 99, 204)
_FILL = QColor(65, 99, 204, 50)
# Digits after the decimal point in the WKT. Eight digits in degrees is
# about one millimetre.
_PRECISION = 8


class ScopeError(ValueError):
    """The chosen scope is not complete yet, for example no area is drawn."""


def _band(canvas: Any) -> QgsRubberBand:
    band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
    band.setStrokeColor(_STROKE)
    band.setFillColor(_FILL)
    band.setWidth(2)
    return band


def _remove_band(canvas: Any, band: QgsRubberBand | None) -> None:
    if band is None:
        return
    band.reset(Qgis.GeometryType.Polygon)
    scene = canvas.scene()
    if scene is not None:
        scene.removeItem(band)


def _wgs84() -> QgsCoordinateReferenceSystem:
    return QgsCoordinateReferenceSystem(WGS84)


def view_bbox(
    extent: QgsRectangle, crs: QgsCoordinateReferenceSystem
) -> tuple[float, float, float, float]:
    """Transform a map extent into a box in EPSG:4326, limited to the world.

    An extent that cannot be transformed gives the whole world.
    """
    transform = QgsCoordinateTransform(crs, _wgs84(), QgsProject.instance())
    transform.setBallparkTransformsAreAppropriate(True)
    try:
        rect = transform.transformBoundingBox(extent)
    except QgsCsException:
        return _WORLD
    if rect.isNull() or not rect.isFinite():
        return _WORLD
    return (
        max(rect.xMinimum(), -180.0),
        max(rect.yMinimum(), -90.0),
        min(rect.xMaximum(), 180.0),
        min(rect.yMaximum(), 90.0),
    )


def area_of(geometry: QgsGeometry, crs: QgsCoordinateReferenceSystem) -> Area:
    """Turn a polygon in ``crs`` into an ``Area`` in EPSG:4326.

    The function drops Z and M values and curves, because the area is only
    a filter in two dimensions.

    Raises:
        ScopeError: The geometry is empty, is not a polygon, or cannot be
            transformed.
    """
    if geometry.isNull() or geometry.isEmpty():
        raise ScopeError("The area is empty.")
    shape = QgsGeometry(geometry)
    if shape.type() != Qgis.GeometryType.Polygon:
        raise ScopeError("The area is not a polygon.")
    if shape.constGet().hasCurvedSegments():
        shape = QgsGeometry(shape.constGet().segmentize())
    if crs.authid() != WGS84:
        transform = QgsCoordinateTransform(crs, _wgs84(), QgsProject.instance())
        transform.setBallparkTransformsAreAppropriate(True)
        try:
            result = shape.transform(transform)
        except QgsCsException as error:
            raise ScopeError(f"Cannot transform the area to {WGS84}: {error}") from error
        if result != Qgis.GeometryOperationResult.Success:
            raise ScopeError(f"Cannot transform the area to {WGS84}.")
    flat = shape.get()
    flat.dropZValue()
    flat.dropMValue()
    rect = shape.boundingBox()
    box = (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
    return Area(shape.asWkt(_PRECISION), box)


def layer_area(layer: QgsVectorLayer, selected_only: bool) -> Area:
    """Join the polygons of a layer into one ``Area`` in EPSG:4326.

    Raises:
        ScopeError: The layer holds no polygon, or the selection is empty.
    """
    request = QgsFeatureRequest().setNoAttributes()
    if selected_only:
        ids = layer.selectedFeatureIds()
        if not ids:
            raise ScopeError(f"Select one or more features in {layer.name()}.")
        request.setFilterFids(ids)
    geometries = [
        feature.geometry()
        for feature in layer.getFeatures(request)
        if feature.hasGeometry() and not feature.geometry().isEmpty()
    ]
    if not geometries:
        raise ScopeError(f"{layer.name()} has no polygon.")
    union = QgsGeometry.unaryUnion(geometries)
    if union.isNull() or union.isEmpty():
        raise ScopeError(f"Cannot join the polygons of {layer.name()}.")
    if union.type() != Qgis.GeometryType.Polygon:
        union = union.convertToType(Qgis.GeometryType.Polygon, True)
    return area_of(union, layer.crs())


def _polygon_filter() -> Any:
    # QGIS 3.34 moved the layer filters to Qgis.LayerFilter. The older enum
    # stays as a fallback for builds that lack it.
    filters = getattr(Qgis, "LayerFilter", None)
    if filters is not None:
        return filters.PolygonLayer
    from qgis.core import QgsMapLayerProxyModel

    return QgsMapLayerProxyModel.Filter.PolygonLayer


class PolygonTool(QgsMapTool):
    """Draw a polygon on the canvas.

    A left click adds a vertex. A right click or a double click finishes
    the polygon, which needs three vertices. A right click with fewer
    vertices cancels, and so does Esc. Backspace removes the last vertex.
    ``finished`` sends the polygon in the canvas CRS.
    """

    finished = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self, canvas: Any):
        super().__init__(canvas)
        self._canvas = canvas
        self._band = _band(canvas)
        self._points: list[QgsPointXY] = []
        self._closing = False
        self._done = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    @property
    def points(self) -> list[QgsPointXY]:
        """The vertices drawn so far, in the canvas CRS."""
        return list(self._points)

    def _show(self, hover: QgsPointXY | None = None) -> None:
        self._band.reset(Qgis.GeometryType.Polygon)
        points = [*self._points, hover] if hover is not None else self._points
        for index, point in enumerate(points):
            self._band.addPoint(point, index == len(points) - 1)

    def canvasMoveEvent(self, event: Any) -> None:
        """Show the next edge under the cursor."""
        if self._points and not self._done:
            self._show(QgsPointXY(event.mapPoint()))

    def canvasDoubleClickEvent(self, event: Any) -> None:
        """Finish on the release that follows the double click.

        The first click of the double click already added the vertex.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self._closing = True

    def canvasReleaseEvent(self, event: Any) -> None:
        """Add a vertex on a left click. Finish on a right click."""
        if self._done:
            return
        if self._closing:
            self._closing = False
            self.finish()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            point = QgsPointXY(event.mapPoint())
            if not self._points or self._points[-1] != point:
                self._points.append(point)
            self._show()
        elif event.button() == Qt.MouseButton.RightButton:
            self.finish()

    def keyPressEvent(self, event: Any) -> None:
        """Cancel on Esc. Remove the last vertex on Backspace."""
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.cancel()
        elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self._points:
            event.accept()
            self._points.pop()
            self._show()
        else:
            event.ignore()

    def finish(self) -> None:
        """Close the polygon and send it, or cancel when it has fewer than three vertices."""
        if self._done:
            return
        if len(self._points) < 3:
            self.cancel()
            return
        self._done = True
        polygon = QgsGeometry.fromPolygonXY([[*self._points, self._points[0]]])
        _remove_band(self._canvas, self._band)
        self.finished.emit(polygon)

    def cancel(self) -> None:
        """Stop drawing and send ``cancelled``."""
        if self._done:
            return
        self._done = True
        _remove_band(self._canvas, self._band)
        self.cancelled.emit()

    def deactivate(self) -> None:
        """Cancel when the user picks another map tool during the drawing."""
        super().deactivate()
        self.cancel()


class ScopePicker(QWidget):
    """Choose which part of a dataset to add or download.

    The choices are "Everything", "Map view", "Draw an area", and "From a
    layer". Drawing uses a polygon map tool on the canvas. The drawn area
    stays on the canvas until the choice changes or ``clear`` runs. Call
    ``clear`` before the picker is deleted, so that no rubber band stays on
    the canvas.
    """

    changed = pyqtSignal()

    def __init__(self, iface: Any, parent: QWidget | None = None):
        super().__init__(parent)
        self._iface = iface
        self._canvas = iface.mapCanvas()
        self._choice = QComboBox()
        for key, label in CHOICES:
            self._choice.addItem(label, key)
        self._current = EVERYTHING
        self._redraw = QToolButton()
        self._redraw.setText("Draw again")
        self._redraw.setToolTip("Draw a new area on the map")
        self._layers = QgsMapLayerComboBox()
        self._layers.setFilters(_polygon_filter())
        self._selected = QCheckBox("Selected features only")
        self._area: Area | None = None
        self._area_band: QgsRubberBand | None = None
        self._tool: PolygonTool | None = None
        self._previous_tool: Any = None
        self._watched: QgsVectorLayer | None = None

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self._choice, 1)
        top.addWidget(self._redraw)
        self._layer_row = QWidget()
        bottom = QHBoxLayout(self._layer_row)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self._layers, 1)
        bottom.addWidget(self._selected)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self._layer_row)

        self._choice.activated.connect(self._activated)
        self._redraw.clicked.connect(self._draw)
        self._layers.layerChanged.connect(self._layer_changed)
        self._selected.toggled.connect(self._layer_input_changed)
        self._canvas.extentsChanged.connect(self._view_changed)
        self._canvas.destinationCrsChanged.connect(self._show_area)
        self._watch(self._layers.currentLayer())
        self._update_rows()

    def choice(self) -> str:
        """Return the key of the current choice, such as ``MAP_VIEW``."""
        return self._current

    def choose(self, key: str) -> None:
        """Select a choice as if the user picked it. ``DRAW`` starts the map tool."""
        index = self._choice.findData(key)
        if index < 0:
            raise ValueError(f"No scope choice {key!r}")
        self._choice.setCurrentIndex(index)
        self._activated(index)

    def scope(self) -> Scope:
        """Return the chosen scope in EPSG:4326.

        Raises:
            ScopeError: The choice is not complete, for example no area is
                drawn, or the layer has no selected feature.
        """
        if self._tool is not None:
            raise ScopeError("Finish the area on the map first.")
        if self._current == MAP_VIEW:
            settings = self._canvas.mapSettings()
            return MapView(view_bbox(self._canvas.extent(), settings.destinationCrs()))
        if self._current == DRAW:
            if self._area is None:
                raise ScopeError("Draw an area on the map first.")
            return self._area
        if self._current == FROM_LAYER:
            layer = self._layers.currentLayer()
            if not isinstance(layer, QgsVectorLayer):
                raise ScopeError("Choose a polygon layer.")
            return layer_area(layer, self._selected.isChecked())
        return Everything()

    def problem(self) -> str | None:
        """Return why ``scope`` cannot give a scope now, or None when it can."""
        try:
            self.scope()
        except ScopeError as error:
            return str(error)
        return None

    def drawing(self) -> bool:
        """Return whether the polygon map tool is active."""
        return self._tool is not None

    def clear(self) -> None:
        """Remove the drawn area from the canvas and restore the map tool.

        A picker that waits for a drawn area goes back to "Everything".
        """
        self._stop_drawing()
        had_area = self._area is not None
        self._drop_area()
        if self._current == DRAW:
            self._set_current(EVERYTHING)
            self.changed.emit()
        elif had_area:
            self.changed.emit()

    def _set_current(self, key: str) -> None:
        self._current = key
        self._choice.setCurrentIndex(self._choice.findData(key))
        self._update_rows()

    def _update_rows(self) -> None:
        self._redraw.setVisible(self._current == DRAW and self._tool is None)
        self._layer_row.setVisible(self._current == FROM_LAYER)

    def _activated(self, index: int) -> None:
        key = str(self._choice.itemData(index))
        if key == DRAW:
            self._draw()
            return
        self._stop_drawing()
        self._drop_area()
        if key != self._current:
            self._set_current(key)
            self.changed.emit()

    def _draw(self) -> None:
        self._stop_drawing()
        tool = PolygonTool(self._canvas)
        tool.finished.connect(self._drawn)
        tool.cancelled.connect(self._draw_cancelled)
        self._previous_tool = self._canvas.mapTool()
        self._tool = tool
        self._update_rows()
        self._canvas.setMapTool(tool)

    def _end_tool(self) -> None:
        tool, self._tool = self._tool, None
        if tool is None:
            return
        with contextlib.suppress(TypeError, RuntimeError):
            tool.finished.disconnect(self._drawn)
        with contextlib.suppress(TypeError, RuntimeError):
            tool.cancelled.disconnect(self._draw_cancelled)
        if self._canvas.mapTool() is tool:
            # Another plugin can delete its map tool while the user draws.
            previous = self._previous_tool
            if previous is not None and not sip.isdeleted(previous):
                self._canvas.setMapTool(previous)
            else:
                self._canvas.unsetMapTool(tool)
        self._previous_tool = None
        tool.deleteLater()

    def _stop_drawing(self) -> None:
        if self._tool is not None:
            # Disconnect first, so the cancel that follows has no effect.
            tool = self._tool
            self._end_tool()
            tool.cancel()
        self._update_rows()

    def _drawn(self, polygon: QgsGeometry) -> None:
        crs = self._canvas.mapSettings().destinationCrs()
        self._end_tool()
        try:
            area = area_of(polygon, crs)
        except ScopeError as error:
            self._iface.messageBar().pushMessage(
                "Portolan Registry", str(error), Qgis.MessageLevel.Warning, 5
            )
            self._draw_cancelled()
            return
        self._area = area
        self._show_area()
        self._set_current(DRAW)
        self.changed.emit()

    def _draw_cancelled(self) -> None:
        self._end_tool()
        if self._area is None:
            # Nothing was drawn, so the earlier choice stays.
            fallback = self._current if self._current != DRAW else EVERYTHING
            self._set_current(fallback)
        else:
            self._set_current(DRAW)

    def _drop_area(self) -> None:
        self._area = None
        _remove_band(self._canvas, self._area_band)
        self._area_band = None

    def _show_area(self) -> None:
        if self._area is None:
            return
        if self._area_band is None:
            self._area_band = _band(self._canvas)
        self._area_band.setToGeometry(QgsGeometry.fromWkt(self._area.wkt), _wgs84())

    def _view_changed(self) -> None:
        if self._current == MAP_VIEW:
            self.changed.emit()

    def _watch(self, layer: QgsMapLayer | None) -> None:
        if self._watched is not None:
            with contextlib.suppress(TypeError, RuntimeError):
                self._watched.selectionChanged.disconnect(self._selection_changed)
        self._watched = layer if isinstance(layer, QgsVectorLayer) else None
        if self._watched is not None:
            self._watched.selectionChanged.connect(self._selection_changed)

    def _layer_changed(self, layer: QgsMapLayer | None) -> None:
        self._watch(layer)
        self._layer_input_changed()

    def _layer_input_changed(self, *_args: object) -> None:
        if self._current == FROM_LAYER:
            self.changed.emit()

    def _selection_changed(self, *_args: object) -> None:
        if self._current == FROM_LAYER and self._selected.isChecked():
            self.changed.emit()
