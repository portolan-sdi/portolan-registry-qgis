"""Read the map canvas, move it, and add layers to the project.

STAC extents are in WGS84 and the canvas has its own CRS, so each function
here transforms between the two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qgis.core import Qgis, QgsCoordinateTransform, QgsProject, QgsRectangle

from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.qgis_io import layers as layer_io
from portolan_registry_qgis.qgis_io import mosaic, parquet_layer
from portolan_registry_qgis.qgis_io.crs import wgs84
from portolan_registry_qgis.qgis_io.log import log

if TYPE_CHECKING:
    from portolan_registry_qgis.core.stac import Bbox


class CanvasError(Exception):
    """The canvas could not transform an extent."""


def canvas_bbox(canvas: Any) -> Bbox | None:
    """Return the extent of ``canvas`` in WGS84, or None when it cannot be transformed."""
    transform = QgsCoordinateTransform(
        canvas.mapSettings().destinationCrs(),
        wgs84(),
        QgsProject.instance(),
    )
    try:
        rect = transform.transformBoundingBox(canvas.extent())
    except Exception:  # noqa: BLE001 - QgsCsException is not importable by name in every QGIS
        return None
    return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())


def zoom_to(canvas: Any, bbox: Bbox) -> None:
    """Show the WGS84 ``bbox`` on ``canvas``.

    Raises:
        CanvasError: The canvas CRS cannot hold the extent.
    """
    transform = QgsCoordinateTransform(
        wgs84(),
        canvas.mapSettings().destinationCrs(),
        QgsProject.instance(),
    )
    try:
        extent = transform.transformBoundingBox(QgsRectangle(*bbox))
    except Exception as error:
        raise CanvasError(str(error)) from error
    canvas.setExtent(extent)
    canvas.refresh()


def _union(layers: list[Any], view: QgsRectangle, target: Any) -> QgsRectangle | None:
    """Return the extent of ``layers`` in the CRS ``target``.

    Returns None when the map ``view`` already shows one of the layers.
    """
    project = QgsProject.instance()
    union = QgsRectangle()
    union.setNull()
    for layer in layers:
        if not layer.isSpatial() or layer.extent().isNull():
            continue
        transform = QgsCoordinateTransform(layer.crs(), target, project)
        try:
            extent = transform.transformBoundingBox(layer.extent())
        except Exception as error:  # noqa: BLE001 - see canvas_bbox
            log(
                _("Could not place {name} on the map: {error}").format(
                    name=layer.name(), error=error
                ),
                Qgis.MessageLevel.Info,
            )
            continue
        if extent.intersects(view):
            return None
        union.combineExtentWith(extent)
    return union


def add_layers(canvas: Any, layers: list[Any]) -> bool:
    """Add ``layers`` to the project, and move the map when it shows none of them.

    A table has no extent and does not move the map. Return whether the map moved.
    """
    target = canvas.mapSettings().destinationCrs()
    view = canvas.extent()
    project = QgsProject.instance()
    for layer in layers:
        project.addMapLayer(layer)
    union = _union(layers, view, target)
    if union is None or union.isNull():
        return False
    if union.isEmpty():
        # One point has no size, so the map keeps its scale.
        canvas.setCenter(union.center())
    else:
        canvas.setExtent(union.buffered(max(union.width(), union.height()) * 0.05))
    canvas.refresh()
    return True


def on_map() -> tuple[set[str], list[str]]:
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
