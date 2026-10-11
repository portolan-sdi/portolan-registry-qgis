"""The part of a dataset that the user wants to add or download.

Every scope is in EPSG:4326. The window transforms the map view or a
polygon into longitude and latitude before it builds a scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from portolan_registry_qgis.core.stac import Bbox


@dataclass(frozen=True)
class Everything:
    """The whole dataset."""


@dataclass(frozen=True)
class MapView:
    """The rectangle the map shows."""

    bbox: Bbox


@dataclass(frozen=True)
class Area:
    """A polygon the user drew or took from a layer.

    ``wkt`` is a Polygon or MultiPolygon. ``bbox`` is its envelope.
    """

    wkt: str
    bbox: Bbox


Scope = Everything | MapView | Area


def bbox_of(scope: Scope) -> Bbox | None:
    """Return the scope's envelope, or None for ``Everything``."""
    if isinstance(scope, (MapView, Area)):
        return scope.bbox
    return None


def _wrap(longitude: float) -> float:
    """Move a longitude outside [-180, 180] into that range."""
    if -180 <= longitude <= 180:
        return longitude
    return (longitude + 180) % 360 - 180


def _spans(west: float, east: float) -> tuple[tuple[float, float], ...]:
    """Return the longitude ranges of a box, each with its west edge first.

    A box with ``west`` greater than ``east`` crosses the antimeridian and
    gives two ranges. A box 360 degrees wide or more covers every longitude.
    A map view can give longitudes past 180 degrees, so they wrap first.
    """
    if west <= east and east - west >= 360:
        return ((-180.0, 180.0),)
    west, east = _wrap(west), _wrap(east)
    if west <= east:
        return ((west, east),)
    return ((west, 180.0), (-180.0, east))


def intersects(bbox: Bbox | None, scope: Scope) -> bool:
    """Return whether a file with extent ``bbox`` can hold data in ``scope``.

    The test compares envelopes only, so an ``Area`` can keep a file whose
    box touches the polygon's envelope but not the polygon. A file with no
    extent is kept, because the test cannot rule it out. ``Everything``
    keeps every file. Boxes that cross the antimeridian are handled.
    Boxes that only touch at an edge intersect.
    """
    area = bbox_of(scope)
    if bbox is None or area is None:
        return True
    west, south, east, north = bbox
    area_west, area_south, area_east, area_north = area
    if south > area_north or north < area_south:
        return False
    return any(
        start <= area_end and end >= area_start
        for start, end in _spans(west, east)
        for area_start, area_end in _spans(area_west, area_east)
    )
