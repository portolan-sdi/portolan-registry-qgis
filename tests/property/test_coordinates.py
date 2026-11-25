from __future__ import annotations

import csv
import io

from hypothesis import example, given
from hypothesis import strategies as st

from portolan_registry_qgis.core.coordinates import coordinate_columns

SEPARATORS = st.sampled_from((",", ";", "\t", "|"))
LONGITUDES = st.floats(-180, 180, allow_nan=False, allow_infinity=False)
LATITUDES = st.floats(-90, 90, allow_nan=False, allow_infinity=False)


@given(
    separator=SEPARATORS,
    longitude=st.sampled_from(("lon", "lng", "longitude")),
    latitude=st.sampled_from(("lat", "latitude")),
    upper=st.booleans(),
    bom=st.booleans(),
)
def test_named_columns_survive_csv_serialization(
    separator: str,
    longitude: str,
    latitude: str,
    *,
    upper: bool,
    bom: bool,
) -> None:
    # Contract: core.coordinates.coordinate_columns and _first_named ignore case.
    names = (longitude.upper(), latitude.upper()) if upper else (longitude, latitude)
    stream = io.StringIO()
    csv.writer(stream, delimiter=separator, quoting=csv.QUOTE_ALL).writerow(("id", *names))
    raw = stream.getvalue().encode("utf-8-sig" if bom else "utf-8")
    assert coordinate_columns(raw) == names


@given(SEPARATORS, st.lists(st.tuples(LONGITUDES, LATITUDES), min_size=1, max_size=20))
@example(",", [(-180.0, -90.0), (180.0, 90.0)])
def test_planar_names_require_all_complete_points_in_degrees(
    separator: str,
    points: list[tuple[float, float]],
) -> None:
    # Contract: core.coordinates._degrees accepts points within inclusive degree limits.
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=separator)
    writer.writerow(("x", "y"))
    writer.writerows(points)
    assert coordinate_columns(stream.getvalue().encode()) == ("x", "y")
    writer.writerow((181, 0))
    assert coordinate_columns(stream.getvalue().encode()) is None


@given(LONGITUDES, LATITUDES, st.integers(181, 1000000), st.integers(91, 1000000))
def test_incomplete_projected_row_does_not_change_detection(
    longitude: float,
    latitude: float,
    x: int,
    y: int,
) -> None:
    # Contract: core.coordinates._complete_rows excludes the unfinished last row.
    raw = f"x,y\n{longitude},{latitude}\n{x},{y}".encode()
    assert coordinate_columns(raw) == ("x", "y")
    assert coordinate_columns(raw + b"\n") is None


@given(st.permutations(("x", "y", "LoN", "LAT", "longitude", "latitude")))
def test_explicit_names_take_priority_over_planar_names(names: list[str]) -> None:
    # Contract: core.coordinates._first_named follows LONGITUDE_NAMES and LATITUDE_NAMES.
    assert coordinate_columns((",".join(names) + "\n").encode()) == ("LoN", "LAT")


@given(st.binary(max_size=1024))
def test_arbitrary_header_bytes_are_safe(raw: bytes) -> None:
    # Contract: core.coordinates.coordinate_columns returns a pair or None.
    result = coordinate_columns(raw)
    assert result is None or (len(result) == 2 and all(isinstance(n, str) for n in result))
