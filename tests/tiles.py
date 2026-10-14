"""The scopes around the two COG items that the core and the in-QGIS tests both read.

Item ``a`` covers 11.0..11.64 east and item ``b`` covers 12.0..12.64 east,
both from 44.0 to 44.64 north.
"""

from __future__ import annotations

from portolan_registry_qgis.core.scope import Area, MapView

WEST_VIEW = MapView((10.9, 43.9, 11.5, 44.7))
EAST_AREA = Area(
    "POLYGON((12.1 44.1, 12.3 44.1, 12.3 44.3, 12.1 44.1))",
    (12.1, 44.1, 12.3, 44.3),
)
# The envelope touches both items. East of 12.0 the triangle stays north of
# item b, so it touches only item a.
TRIANGLE = Area(
    "POLYGON((11.5 44.3, 12.3 44.9, 11.5 44.9, 11.5 44.3))",
    (11.5, 44.3, 12.3, 44.9),
)
