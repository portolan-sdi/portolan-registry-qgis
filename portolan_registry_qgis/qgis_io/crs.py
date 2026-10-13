"""The CRS of every scope, bbox, and STAC extent: EPSG:4326."""

from __future__ import annotations

from qgis.core import QgsCoordinateReferenceSystem

WGS84 = "EPSG:4326"


def wgs84() -> QgsCoordinateReferenceSystem:
    """Return a new EPSG:4326 CRS.

    A function and not a constant, because a CRS made at import time can
    come before QGIS starts. Such a CRS is not valid.
    """
    return QgsCoordinateReferenceSystem(WGS84)
