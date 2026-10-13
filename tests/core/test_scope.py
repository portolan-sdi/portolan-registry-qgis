from __future__ import annotations

import pytest

from portolan_registry_qgis.core.scope import (
    ANTIMERIDIAN_SIDES,
    Area,
    Everything,
    MapView,
    bbox_of,
    crosses_antimeridian,
    eastward,
    intersects,
    split_bbox,
)

SQUARE = "POLYGON ((10 40, 12 40, 12 42, 10 42, 10 40))"


def test_bbox_of():
    assert bbox_of(Everything()) is None
    assert bbox_of(MapView((1, 2, 3, 4))) == (1, 2, 3, 4)
    assert bbox_of(Area(SQUARE, (10, 40, 12, 42))) == (10, 40, 12, 42)


def test_everything_and_files_without_an_extent_are_kept():
    assert intersects((0, 0, 1, 1), Everything())
    assert intersects(None, Everything())
    assert intersects(None, MapView((50, 50, 51, 51)))


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        ((11, 41, 13, 43), True),
        ((0, 0, 1, 1), False),
        # Inside in longitude, outside in latitude.
        ((10.5, 50, 11.5, 51), False),
        # The file covers the whole scope.
        ((-180, -90, 180, 90), True),
        # Boxes that touch at an edge intersect, as in the SQL filter.
        ((12, 42, 13, 43), True),
        ((12.001, 41, 13, 43), False),
    ],
)
def test_intersects_a_map_view(bbox, expected):
    assert intersects(bbox, MapView((10, 40, 12, 42))) is expected


def test_an_area_compares_envelopes_only():
    area = Area("POLYGON ((10 40, 12 40, 10 42, 10 40))", (10, 40, 12, 42))
    # The box misses the triangle but touches its envelope, so it is kept.
    assert intersects((11.5, 41.5, 12, 42), area)
    assert not intersects((20, 40, 21, 42), area)


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        # A file that crosses the antimeridian, from 170 E to 170 W.
        ((170, -20, -170, -10), True),
        ((170, 10, -170, 20), False),
    ],
)
def test_a_file_that_crosses_the_antimeridian(bbox, expected):
    assert intersects(bbox, MapView((175, -18, 178, -12))) is expected
    assert intersects(bbox, MapView((-178, -18, -175, -12))) is expected


def test_a_file_that_crosses_the_antimeridian_misses_the_rest_of_the_world():
    fiji = (177, -19, -178, -16)
    assert not intersects(fiji, MapView((0, -20, 10, -10)))
    assert not intersects(fiji, MapView((-170, -20, -160, -10)))


def test_a_scope_that_crosses_the_antimeridian():
    scope = Area("MULTIPOLYGON (((170 -20, 180 -20, 180 -10, 170 -20)))", (170, -20, -170, -10))
    assert intersects((178, -15, 179, -14), scope)
    assert intersects((-179, -15, -178, -14), scope)
    assert not intersects((0, -15, 1, -14), scope)
    # Both boxes cross the antimeridian.
    assert intersects((175, -15, -175, -14), scope)


def test_a_map_view_past_180_degrees_wraps():
    # A map that pans east past the antimeridian can report longitudes above 180.
    view = MapView((170, -20, 190, -10))
    assert intersects((-175, -15, -174, -14), view)
    assert intersects((172, -15, 174, -14), view)
    assert not intersects((-160, -15, -150, -14), view)
    assert intersects((-160, -15, -150, -14), MapView((-200, -20, 220, -10)))


@pytest.mark.parametrize(
    ("longitudes", "projected", "expected"),
    [
        # Panned past 180° on a canvas in degrees.
        ([177, 182, 182, 177, 177], False, True),
        ([-182, -177, -177, -182], False, True),
        # A wide polygon drawn in degrees is what the user drew.
        ([-170, 170, 170, -170, -170], False, False),
        # A projected polygon whose edge jumps across 180° after the transform.
        ([177, -178, -178, 177, 177], True, True),
        ([10, 12, 12, 10, 10], True, False),
    ],
)
def test_crosses_antimeridian(longitudes, projected, expected):
    assert crosses_antimeridian(longitudes, projected=projected) is expected


def test_eastward():
    assert eastward(-178) == 182
    assert eastward(0) == 0
    assert eastward(177) == 177


def test_split_bbox():
    assert split_bbox((177, -19, 182, -16)) == (177, -19, -178, -16)
    # A box west of 180° after the move does not cross it.
    assert split_bbox((10, 0, 180, 1)) is None
    assert split_bbox((-185, 0, 190, 1)) == (-180, 0, -170, 1)


def test_antimeridian_sides_bring_each_side_into_range():
    (west, west_shift), (east, east_shift) = ANTIMERIDIAN_SIDES
    assert (west[0] + west_shift, west[2] + west_shift) == (-180, 180)
    assert (east[0] + east_shift, east[2] + east_shift) == (-180, 180)
