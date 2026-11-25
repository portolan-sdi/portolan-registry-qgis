"""Property tests for the scope of a download or a load."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import scope
from tests.property import strategies

if TYPE_CHECKING:
    from portolan_registry_qgis.core.stac import Bbox

# Quarter degrees, so every sum and wrap below is exact in binary floating point.
inner_longitudes = st.integers(-719, 719).map(lambda n: n / 4)
longitudes = st.integers(-720, 720).map(lambda n: n / 4)
latitudes = st.integers(-360, 360).map(lambda n: n / 4)
grid_boxes = strategies.boxes(longitudes, latitudes)


scopes = st.one_of(
    st.builds(scope.MapView, grid_boxes),
    st.builds(lambda box: scope.Area("POLYGON EMPTY", box), grid_boxes),
)


@given(grid_boxes, grid_boxes)
def test_intersects_is_symmetric(a: Bbox, b: Bbox) -> None:
    assert scope.intersects(a, scope.MapView(b)) == scope.intersects(b, scope.MapView(a))
    assert scope.intersects(a, scope.MapView(b)) == scope.intersects(a, scope.Area("", b))


@given(grid_boxes, scopes)
def test_a_box_meets_itself_and_everything_holds_all(box: Bbox, chosen: scope.Scope) -> None:
    assert scope.intersects(box, scope.MapView(box))
    assert scope.intersects(box, scope.Everything())
    assert scope.intersects(None, chosen)


@given(strategies.boxes(inner_longitudes, latitudes), grid_boxes, st.sampled_from((-360.0, 360.0)))
def test_a_turn_of_360_degrees_changes_nothing(box: Bbox, view: Bbox, turn: float) -> None:
    west, south, east, north = box
    turned = (west + turn, south, east + turn, north)
    assert scope.intersects(turned, scope.MapView(view)) == scope.intersects(
        box, scope.MapView(view)
    )
    assert scope.intersects(view, scope.MapView(turned)) == scope.intersects(
        view, scope.MapView(box)
    )


@given(
    st.integers(0, 720).map(lambda n: n / 4), st.integers(-719, -1).map(lambda n: n / 4), latitudes
)
def test_split_bbox_undoes_eastward(west: float, east: float, lat: float) -> None:
    # RFC 7946 section 5.2, "The Antimeridian": a box that crosses it has a
    # westernmost longitude greater than its easternmost longitude.
    moved = (scope.eastward(west), lat, scope.eastward(east), lat)
    assert scope.split_bbox(moved) == (west, lat, east, lat)
    assert scope.split_bbox((west, lat, west, lat)) is None


@given(grid_boxes)
def test_clip_wkt_covers_the_view_in_one_or_two_closed_boxes(box: Bbox) -> None:
    west, south, east, north = box
    wkt = scope.clip_wkt(scope.MapView(box))
    assert wkt is not None
    rings = re.findall(r"\(\(([^()]*)\)\)", wkt)
    assert len(rings) == (2 if west > east else 1)
    spans = []
    for ring in rings:
        points = [tuple(float(n) for n in point.split()) for point in ring.split(",")]
        assert points[0] == points[-1]
        assert {y for _x, y in points} == {south, north}
        spans.append((min(x for x, _y in points), max(x for x, _y in points)))
    assert spans[0][0] == west
    assert spans[-1][1] == east
