from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from portolan_registry_qgis.core.footprint import covers, rects, window
from tests.tiles import EAST_AREA, WEST_VIEW

if TYPE_CHECKING:
    from hypothesis.strategies import DrawFn

    from portolan_registry_qgis.core.stac import Bbox

WORLD = (-180.0, -90.0, 180.0, 90.0)


@st.composite
def boxes(draw: DrawFn) -> Bbox:
    west, east = sorted(draw(st.tuples(st.floats(-180, 180), st.floats(-180, 180))))
    south, north = sorted(draw(st.tuples(st.floats(-90, 90), st.floats(-90, 90))))
    return west, south, east, north


@given(st.one_of(boxes(), st.sampled_from((EAST_AREA.bbox, WEST_VIEW.bbox))), st.floats(0.1, 10))
@example(WORLD, 2.0)
@example((180.0, 90.0, 180.0, 90.0), 2.0)
@example((-180.0, -90.0, -180.0, -90.0), 0.1)
def test_window_is_ordered_bounded_and_contains_extent(bbox: Bbox, aspect: float) -> None:
    # Contract: core.footprint.window returns the area shown around the extent.
    west, south, east, north = window(bbox, aspect)
    assert -180 - 1e-10 <= west < east <= 180 + 1e-10
    assert -90 - 1e-10 <= south < north <= 90 + 1e-10
    assert west <= bbox[0] + 1e-10
    assert bbox[2] <= east + 1e-10
    assert south <= bbox[1] + 1e-10
    assert bbox[3] <= north + 1e-10


@given(boxes(), st.integers(1, 4096), st.integers(1, 4096))
def test_pixel_projection_round_trip(bbox: Bbox, width: int, height: int) -> None:
    # Contract: core.footprint.rects maps degrees to (x, y, w, h), with y down.
    (rectangle,) = rects(bbox, WORLD, width, height)
    x, y, span_x, span_y = rectangle
    restored = (
        x / width * 360 - 180,
        90 - (y + span_y) / height * 180,
        (x + span_x) / width * 360 - 180,
        90 - y / height * 180,
    )
    assert restored == pytest.approx(bbox, abs=1e-10)
    assert span_x >= 0
    assert span_y >= 0


@given(st.floats(0.01, 180), st.floats(-180, -0.01), st.integers(1, 4096))
def test_antimeridian_splits_preserve_width(west: float, east: float, width: int) -> None:
    # Contract: core.footprint.rects splits an antimeridian extent at the world edges.
    bbox = (west, -90.0, east, 90.0)
    right, left = rects(bbox, WORLD, width, 180)
    assert right[0] + right[2] == pytest.approx(width)
    assert left[0] == 0
    assert right[2] + left[2] == pytest.approx((360 - west + east) / 360 * width)
    assert right[1::2] == left[1::2] == (0, 180)
    assert window(bbox, 2) == WORLD


@given(boxes(), st.floats(0, 1), st.floats(0, 1))
def test_cover_threshold_is_monotone(bbox: Bbox, first: float, second: float) -> None:
    # Contract: core.footprint.covers compares the overlap area to share of the view.
    low, high = sorted((first, second))
    assert not covers(bbox, WORLD, high) or covers(bbox, WORLD, low)
    assert covers(WORLD, bbox)


@given(st.floats(0.1, 10))
def test_missing_extent_respects_aspect_and_world_edges(aspect: float) -> None:
    # Contract: core.footprint.window uses the picture aspect for a missing extent.
    west, south, east, north = window(None, aspect)
    assert -180 - 1e-10 <= west < east <= 180 + 1e-10
    assert -90 - 1e-10 <= south < north <= 90 + 1e-10
    assert (east - west) / (north - south) == pytest.approx(aspect)
