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
    raise NotImplementedError("Stage 1 core stream")


def intersects(bbox: Bbox | None, scope: Scope) -> bool:
    """Return whether a file with extent ``bbox`` can hold data in ``scope``.

    The test compares envelopes only, so an ``Area`` can keep a file whose
    box touches the polygon's envelope but not the polygon. A file with no
    extent is kept, because the test cannot rule it out. ``Everything``
    keeps every file. Boxes that cross the antimeridian are handled.
    """
    raise NotImplementedError("Stage 1 core stream")
