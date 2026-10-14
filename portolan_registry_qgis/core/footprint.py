"""Place a STAC extent on a small plate carrée picture of the world."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from portolan_registry_qgis.core.stac import Bbox

Rect = tuple[float, float, float, float]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def window(bbox: Bbox | None, aspect: float, min_span: float = 30.0, context: float = 2.5) -> Bbox:
    """Return the area a picture of ``bbox`` shows, in degrees.

    The area centers on ``bbox`` with the picture's ``aspect``. A missing
    extent, or one that crosses the antimeridian, shows the world.
    """
    if bbox is None or bbox[0] > bbox[2]:
        width = min(360.0, 180.0 * aspect)
        height = width / aspect
        return (-width / 2, -height / 2, width / 2, height / 2)
    west, south, east, north = bbox
    width = max((east - west) * context, min_span)
    height = max((north - south) * context, min_span / aspect)
    if width / height < aspect:
        width = height * aspect
    else:
        height = width / aspect
    width, height = min(width, 360.0), min(height, 180.0)
    cx = _clamp((west + east) / 2, -180 + width / 2, 180 - width / 2)
    cy = _clamp((south + north) / 2, -90 + height / 2, 90 - height / 2)
    return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)


def rects(bbox: Bbox, view: Bbox, width: float, height: float) -> tuple[Rect, ...]:
    """Return ``bbox`` as ``(x, y, w, h)`` pixel rectangles in a picture of ``view``.

    The y axis points down, as in an image. An extent that crosses the
    antimeridian returns two rectangles, one on each side of the world.
    """
    west, south, east, north = bbox
    if west > east:
        return rects((west, south, 180.0, north), view, width, height) + rects(
            (-180.0, south, east, north), view, width, height
        )
    sx = width / (view[2] - view[0])
    sy = height / (view[3] - view[1])
    return (
        ((west - view[0]) * sx, (view[3] - north) * sy, (east - west) * sx, (north - south) * sy),
    )


def _overlap(low: float, high: float, view_low: float, view_high: float) -> float:
    return max(0.0, min(high, view_high) - max(low, view_low))


def covers(bbox: Bbox, view: Bbox, share: float = 0.8) -> bool:
    """Return whether ``bbox`` fills at least ``share`` of ``view``.

    A box that fills the picture tells nothing, so the panel tints the land
    instead of drawing it.
    """
    west, south, east, north = bbox
    spans = [(west, 180.0), (-180.0, east)] if west > east else [(west, east)]
    width = sum(_overlap(low, high, view[0], view[2]) for low, high in spans)
    height = _overlap(south, north, view[1], view[3])
    return width * height >= (view[2] - view[0]) * (view[3] - view[1]) * share
