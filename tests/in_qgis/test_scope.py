from __future__ import annotations

import pytest
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapMouseEvent, QgsMapToolPan, QgsMessageBar
from qgis.PyQt.QtCore import QEvent, QPoint, Qt
from qgis.PyQt.QtGui import QKeyEvent

from portolan_registry_qgis.core.scope import Area, Everything, MapView
from portolan_registry_qgis.gui import scope as scope_module
from portolan_registry_qgis.gui.scope import (
    DRAW,
    EVERYTHING,
    FROM_LAYER,
    MAP_VIEW,
    PolygonTool,
    ScopeError,
    ScopePicker,
    area_of,
    layer_area,
    view_bbox,
)


# A CRS made at import time, before the qgis_app fixture starts QGIS, breaks
# every later CRS lookup on QGIS 3.34. These functions make each CRS on use.
def mercator():
    return QgsCoordinateReferenceSystem("EPSG:3857")


def wgs84():
    return QgsCoordinateReferenceSystem("EPSG:4326")


class FakeIface:
    """The parts of QgisInterface the picker calls."""

    def __init__(self):
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(wgs84())
        self.canvas.resize(400, 400)
        self.canvas.setExtent(QgsRectangle(10, 43, 14, 46))
        self.bar = QgsMessageBar()

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar


@pytest.fixture
def iface():
    fake = FakeIface()
    yield fake
    QgsProject.instance().clear()
    fake.canvas.deleteLater()
    fake.bar.deleteLater()
    QgsApplication.processEvents()


@pytest.fixture
def picker(iface):
    widget = ScopePicker(iface)
    events = []
    widget.changed.connect(lambda: events.append(widget.choice()))
    widget.events = events
    yield widget
    widget.release()
    widget.deleteLater()


@pytest.fixture
def pan(iface):
    tool = QgsMapToolPan(iface.canvas)
    iface.canvas.setMapTool(tool)
    yield tool
    iface.canvas.unsetMapTool(tool)


def _mouse(canvas, kind, button, x, y):
    event = QgsMapMouseEvent(
        canvas, kind, QPoint(0, 0), button, button, Qt.KeyboardModifier.NoModifier
    )
    event.setMapPoint(QgsPointXY(x, y))
    return event


def _click(tool, canvas, x, y, button=Qt.MouseButton.LeftButton):
    tool.canvasReleaseEvent(_mouse(canvas, QEvent.Type.MouseButtonRelease, button, x, y))


def _key(tool, key):
    tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))


def _bands(canvas):
    return [item for item in canvas.scene().items() if isinstance(item, scope_module.QgsRubberBand)]


def test_everything_is_the_default(picker):
    assert picker.choice() == EVERYTHING
    assert picker.scope() == Everything()
    assert picker.problem() is None


def test_map_view(picker, iface):
    picker.choose(MAP_VIEW)
    assert picker.events == [MAP_VIEW]
    scope = picker.scope()
    assert isinstance(scope, MapView)
    extent = iface.canvas.extent()
    assert scope.bbox == pytest.approx(
        (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())
    )
    # A pan in map view mode changes the scope.
    iface.canvas.setExtent(QgsRectangle(11, 44, 12, 45))
    assert picker.events[-1] == MAP_VIEW
    assert len(picker.events) >= 2
    picker.choose(EVERYTHING)
    count = len(picker.events)
    iface.canvas.setExtent(QgsRectangle(10, 43, 14, 46))
    assert len(picker.events) == count


def test_view_bbox_transforms_and_limits_to_the_world():
    box = QgsCoordinateTransform(wgs84(), mercator(), QgsProject.instance()).transformBoundingBox(
        QgsRectangle(11, 44, 12, 45)
    )
    assert view_bbox(box, mercator()) == pytest.approx((11, 44, 12, 45))
    assert view_bbox(QgsRectangle(-500, -100, 500, 100), wgs84()) == (-180, -90, 180, 90)


def _draw_square(picker, iface):
    picker.choose(DRAW)
    tool = iface.canvas.mapTool()
    assert isinstance(tool, PolygonTool)
    for x, y in ((11, 44), (12, 44), (12, 45), (11, 45)):
        _click(tool, iface.canvas, x, y)
    return tool


def test_draw_an_area(picker, iface, pan):
    tool = _draw_square(picker, iface)
    assert picker.drawing()
    assert picker.problem() == "Finish the area on the map first."
    _click(tool, iface.canvas, 13, 46, Qt.MouseButton.RightButton)
    assert iface.canvas.mapTool() is pan
    assert not picker.drawing()
    scope = picker.scope()
    assert isinstance(scope, Area)
    assert scope.bbox == pytest.approx((11, 44, 12, 45))
    assert QgsGeometry.fromWkt(scope.wkt).equals(
        QgsGeometry.fromWkt("POLYGON((11 44, 12 44, 12 45, 11 45, 11 44))")
    )
    assert picker.events == [DRAW]
    # The finished polygon stays on the canvas until the choice changes.
    assert len(_bands(iface.canvas)) == 1
    picker.choose(MAP_VIEW)
    assert _bands(iface.canvas) == []
    assert isinstance(picker.scope(), MapView)


def test_double_click_finishes(picker, iface, pan):
    picker.choose(DRAW)
    tool = iface.canvas.mapTool()
    for x, y in ((11, 44), (12, 44), (12, 45)):
        _click(tool, iface.canvas, x, y)
    # Qt sends the second click of a double click as a double click and a release.
    tool.canvasDoubleClickEvent(
        _mouse(iface.canvas, QEvent.Type.MouseButtonDblClick, Qt.MouseButton.LeftButton, 12, 45)
    )
    _click(tool, iface.canvas, 12, 45)
    scope = picker.scope()
    assert QgsGeometry.fromWkt(scope.wkt).constGet().nCoordinates() == 4
    assert iface.canvas.mapTool() is pan


def test_draw_in_another_crs(picker, iface, pan):
    iface.canvas.setDestinationCrs(mercator())
    to_mercator = QgsCoordinateTransform(wgs84(), mercator(), QgsProject.instance())
    picker.choose(DRAW)
    tool = iface.canvas.mapTool()
    for x, y in ((11, 44), (12, 44), (12, 45)):
        point = to_mercator.transform(QgsPointXY(x, y))
        _click(tool, iface.canvas, point.x(), point.y())
    tool.finish()
    assert picker.scope().bbox == pytest.approx((11, 44, 12, 45))


def test_escape_cancels_and_keeps_the_earlier_choice(picker, iface, pan):
    picker.choose(MAP_VIEW)
    tool = _draw_square(picker, iface)
    _key(tool, Qt.Key.Key_Escape)
    assert iface.canvas.mapTool() is pan
    assert picker.choice() == MAP_VIEW
    assert isinstance(picker.scope(), MapView)
    assert _bands(iface.canvas) == []
    assert picker.events == [MAP_VIEW]


def test_too_few_vertices_cancel(picker, iface, pan):
    picker.choose(DRAW)
    tool = iface.canvas.mapTool()
    _click(tool, iface.canvas, 11, 44)
    _click(tool, iface.canvas, 12, 44)
    _key(tool, Qt.Key.Key_Backspace)
    assert len(tool.points) == 1
    _click(tool, iface.canvas, 12, 44, Qt.MouseButton.RightButton)
    assert iface.canvas.mapTool() is pan
    assert picker.choice() == EVERYTHING
    assert picker.events == []


def test_another_map_tool_cancels_the_drawing(picker, iface, pan):
    _draw_square(picker, iface)
    other = QgsMapToolPan(iface.canvas)
    iface.canvas.setMapTool(other)
    assert not picker.drawing()
    assert picker.choice() == EVERYTHING
    assert iface.canvas.mapTool() is other
    # The picker sets no tool of its own during the canvas's tool change.
    # Before, it set the pan tool again inside that change, and the pan tool
    # stayed active next to the new tool.
    assert not pan.isActive()
    assert other.isActive()
    iface.canvas.unsetMapTool(other)


def test_draw_again_keeps_the_area_on_cancel(picker, iface, pan):
    tool = _draw_square(picker, iface)
    tool.finish()
    first = picker.scope()
    picker.redraw.click()
    tool = iface.canvas.mapTool()
    assert isinstance(tool, PolygonTool)
    tool.cancel()
    assert picker.scope() == first
    assert len(_bands(iface.canvas)) == 1


def test_clear_restores_the_map_tool(picker, iface, pan):
    _draw_square(picker, iface)
    picker.clear()
    assert iface.canvas.mapTool() is pan
    assert picker.choice() == EVERYTHING
    tool = _draw_square(picker, iface)
    tool.finish()
    assert len(_bands(iface.canvas)) == 1
    picker.clear()
    assert _bands(iface.canvas) == []
    assert picker.scope() == Everything()
    assert picker.events[-1] == EVERYTHING


def _squares(crs="EPSG:3857"):
    layer = QgsVectorLayer(f"Polygon?crs={crs}", "squares", "memory")
    to_layer = QgsCoordinateTransform(wgs84(), layer.crs(), QgsProject.instance())
    features = []
    for west in (11, 13):
        geometry = QgsGeometry.fromRect(QgsRectangle(west, 44, west + 1, 45))
        geometry.transform(to_layer)
        feature = QgsFeature()
        feature.setGeometry(geometry)
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return layer


def test_from_a_layer(picker, iface):
    layer = _squares()
    points = QgsVectorLayer("Point?crs=EPSG:4326", "points", "memory")
    QgsProject.instance().addMapLayers([points, layer])
    picker.choose(FROM_LAYER)
    picker.layer_combo.setLayer(layer)
    assert picker.events[0] == FROM_LAYER
    scope = picker.scope()
    assert isinstance(scope, Area)
    assert scope.bbox == pytest.approx((11, 44, 14, 45))
    assert QgsGeometry.fromWkt(scope.wkt).isMultipart()

    picker.selected.setChecked(True)
    assert picker.problem() == "Select one or more features in squares."
    count = len(picker.events)
    first = next(layer.getFeatures()).id()
    layer.selectByIds([first])
    assert len(picker.events) == count + 1
    assert picker.scope().bbox == pytest.approx((11, 44, 12, 45))


def test_the_layer_union_is_kept_until_the_layer_changes(picker, monkeypatch):
    layer = _squares()
    QgsProject.instance().addMapLayer(layer)
    real = scope_module.layer_area
    calls = []

    def counted(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(scope_module, "layer_area", counted)
    picker.choose(FROM_LAYER)
    picker.layer_combo.setLayer(layer)
    # The window asks for the scope on each button update. The union reads
    # every feature, so it runs once.
    for _ in range(5):
        assert picker.problem() is None
        picker.scope()
    assert len(calls) == 1
    # A new feature changes the area.
    feature = QgsFeature()
    to_layer = QgsCoordinateTransform(wgs84(), layer.crs(), QgsProject.instance())
    square = QgsGeometry.fromRect(QgsRectangle(15, 44, 16, 45))
    square.transform(to_layer)
    feature.setGeometry(square)
    layer.startEditing()
    layer.addFeature(feature)
    assert picker.scope().bbox == pytest.approx((11, 44, 16, 45))
    assert len(calls) == 2
    layer.rollBack()
    # The selection matters only with "Selected features only".
    layer.selectByIds([next(layer.getFeatures()).id()])
    picker.scope()
    assert len(calls) == 3
    picker.selected.setChecked(True)
    assert picker.scope().bbox == pytest.approx((11, 44, 12, 45))
    layer.selectAll()
    assert picker.scope().bbox == pytest.approx((11, 44, 14, 45))
    assert len(calls) == 5


def test_an_area_across_the_antimeridian_splits_in_two():
    # A box around Fiji, drawn on a Pacific-centred map.
    pacific = QgsCoordinateReferenceSystem("EPSG:3832")
    ring = [QgsPointXY(x, y) for x, y in ((177, -19), (182, -19), (182, -16), (177, -16))]
    geometry = QgsGeometry.fromPolygonXY([[*ring, ring[0]]])
    geometry.transform(QgsCoordinateTransform(wgs84(), pacific, QgsProject.instance()))
    area = area_of(geometry, pacific)
    west, south, east, north = area.bbox
    assert (west, east) == (pytest.approx(177, abs=1e-6), pytest.approx(-178, abs=1e-6))
    assert (south, north) == (pytest.approx(-19, abs=1e-6), pytest.approx(-16, abs=1e-6))
    shape = QgsGeometry.fromWkt(area.wkt)
    assert shape.isMultipart()
    # The area holds Fiji on both sides of 180°, and not the rest of the world.
    cases = ((179, True), (-179, True), (0, False), (-170, False), (170, False))
    for x, inside in cases:
        point = QgsGeometry.fromPointXY(QgsPointXY(x, -17))
        assert shape.intersects(point) is inside, x
    assert shape.area() == pytest.approx(5 * 3, rel=1e-3)


def test_an_area_past_180_degrees_splits_in_two():
    # A canvas in degrees, panned east past 180°.
    geometry = QgsGeometry.fromRect(QgsRectangle(177, -19, 182, -16))
    area = area_of(geometry, wgs84())
    assert area.bbox == pytest.approx((177, -19, -178, -16))
    assert QgsGeometry.fromWkt(area.wkt).isMultipart()


def test_a_wide_area_that_does_not_cross_stays_whole():
    geometry = QgsGeometry.fromRect(QgsRectangle(-170, -10, 170, 10))
    area = area_of(geometry, wgs84())
    assert area.bbox == pytest.approx((-170, -10, 170, 10))
    assert not QgsGeometry.fromWkt(area.wkt).isMultipart()


def test_the_layer_list_holds_polygon_layers_only(picker):
    polygons = _squares()
    points = QgsVectorLayer("Point?crs=EPSG:4326", "points", "memory")
    QgsProject.instance().addMapLayers([points, polygons])
    combo = picker.layer_combo
    names = [combo.itemText(i) for i in range(combo.count())]
    assert names == ["squares"]


def test_no_layer_is_a_problem(picker):
    picker.choose(FROM_LAYER)
    assert picker.problem() == "Choose a polygon layer."


def test_area_of_rejects_what_is_not_a_polygon():
    with pytest.raises(ScopeError, match="not a polygon"):
        area_of(QgsGeometry.fromWkt("LINESTRING(0 0, 1 1)"), wgs84())
    with pytest.raises(ScopeError, match="empty"):
        area_of(QgsGeometry(), wgs84())
    area = area_of(QgsGeometry.fromWkt("POLYGON Z((0 0 5, 1 0 5, 1 1 5, 0 0 5))"), wgs84())
    assert "Z" not in area.wkt
    curved = area_of(
        QgsGeometry.fromWkt("CURVEPOLYGON(CIRCULARSTRING(0 0, 1 1, 2 0, 1 -1, 0 0))"), wgs84()
    )
    assert curved.wkt.startswith("Polygon")


def test_layer_area_of_an_empty_layer():
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "empty", "memory")
    with pytest.raises(ScopeError, match="has no polygon"):
        layer_area(layer, False)
