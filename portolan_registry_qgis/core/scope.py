"""The part of a dataset that the user wants to add or download.

Every scope is in EPSG:4326. The window transforms the map view or a
polygon into longitude and latitude before it builds a scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
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


# The two sides of an area that crosses 180°, after ``eastward`` moves its
# vertices. Each side is a box and the shift in longitude that brings it
# into -180..180.
ANTIMERIDIAN_SIDES: tuple[tuple[Bbox, float], ...] = (
    ((-180.0, -90.0, 180.0, 90.0), 0.0),
    ((180.0, -90.0, 540.0, 90.0), -360.0),
)


def crosses_antimeridian(longitudes: list[float], *, projected: bool) -> bool:
    """Return whether a polygon with these vertex longitudes can cross 180°.

    A polygon crosses the antimeridian in two cases:
    - It has longitudes beyond ±180°, as on a canvas in degrees that the user
      panned past 180°.
    - It came from a projected CRS, and two neighbouring vertices differ by
      more than 180° of longitude after the transform. The caller densifies
      the edges of a wide polygon before the transform, so each edge is
      short.

    A polygon drawn in degrees with longitudes in -180..180 is what the user
    drew, even when it is wide.
    """
    if any(longitude < -180 or longitude > 180 for longitude in longitudes):
        return True
    return projected and _jumps(longitudes)


def _jumps(longitudes: list[float]) -> bool:
    """Return whether two neighbouring longitudes differ by more than 180°."""
    return any(abs(east - west) > 180 for west, east in pairwise(longitudes))


def eastward(longitude: float) -> float:
    """Move a negative longitude 360° east, so a polygon across 180° is in one piece."""
    return longitude + 360 if longitude < 0 else longitude


def split_bbox(moved: Bbox) -> Bbox | None:
    """Return the box of an area that ``eastward`` moved, as STAC writes it.

    The box has west > east when the area crosses 180°. The function
    returns None when the moved area stays west of 180°, so it does not
    cross.
    """
    west, south, east, north = moved
    if east <= 180:
        return None
    return (max(west, -180), south, east - 360, north)
