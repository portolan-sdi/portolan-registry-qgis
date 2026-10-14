from __future__ import annotations

import pytest

from portolan_registry_qgis.core.coordinates import coordinate_columns


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"id,lon,lat\n1,2,3\n", ("lon", "lat")),
        (b"\xef\xbb\xbfID;Longitude;Latitude\n", ("Longitude", "Latitude")),
        (b"name\tX\tY\na\t11.2\t44.5\n", ("X", "Y")),
        # x and y in RD New are not degrees, so the CSV opens as a table.
        (b"id,x,y\n1,155000,463000\n", None),
        # x and y with no complete row cannot be told apart.
        (b"name\tX\tY\n", None),
        (b"id,x,y\n1,11.2,44.5\n2,155000,4630", ("x", "y")),
        # A short row and a row that is not numbers do not count.
        (b"id,x,y\n1\nn,a,b\n2,11.2,44.5\n", ("x", "y")),
        (b'"lng","LAT"\n', ("lng", "LAT")),
        # "lon" comes before "x" in the order of preference.
        (b"x,y,lon,lat\n", ("lon", "lat")),
        # Two columns with the same folded name give the first.
        (b"Lon,lon,lat\n", ("Lon", "lat")),
        (b"id,lon,name\n", None),
        (b"id,name\n", None),
        (b"", None),
    ],
)
def test_coordinate_columns(header, expected):
    assert coordinate_columns(header) == expected
