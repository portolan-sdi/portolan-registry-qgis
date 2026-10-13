"""Find the longitude and latitude columns of a CSV from its first bytes."""

from __future__ import annotations

import csv

# Header names that hold point coordinates in EPSG:4326, in order of
# preference. The comparison ignores case.
LONGITUDE_NAMES = ("lon", "lng", "longitude", "x")
LATITUDE_NAMES = ("lat", "latitude", "y")
# Many CSVs use x and y for projected coordinates. These names count only
# when the rows in the header bytes hold longitudes and latitudes.
_PLANAR_NAMES = frozenset({"x", "y"})
# The separators that GDAL's CSV driver detects in the header line.
_CSV_SEPARATORS = (",", ";", "\t", "|")


def coordinate_columns(header: bytes) -> tuple[str, str] | None:
    """Find the longitude and latitude columns in the first line of a CSV.

    Columns named x and y count only when every complete row in ``header``
    holds a longitude and a latitude, because x and y are often projected.

    Args:
        header: The start of the file. The first line names the columns.
            The complete rows after it test x and y.

    Returns:
        The exact names of the longitude and latitude columns, or None when
        the header does not name both.
    """
    text = header.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    if not lines:
        return None
    separator = max(_CSV_SEPARATORS, key=lines[0].count)
    rows = list(csv.reader(lines, delimiter=separator))
    names = [name.strip() for name in rows[0]]
    lon = _first_named(names, LONGITUDE_NAMES)
    lat = _first_named(names, LATITUDE_NAMES)
    if lon is None or lat is None:
        return None
    if {lon.casefold(), lat.casefold()} & _PLANAR_NAMES:
        # The last line can stop in the middle of a row.
        complete = rows[1:-1] if not text.endswith(("\n", "\r")) else rows[1:]
        if not _degrees(complete, names.index(lon), names.index(lat)):
            return None
    return lon, lat


def _first_named(names: list[str], wanted: tuple[str, ...]) -> str | None:
    """Return the column whose name comes first in ``wanted``, ignoring case.

    Two columns with the same folded name give the first of them.
    """
    by_folded: dict[str, str] = {}
    for name in names:
        by_folded.setdefault(name.casefold(), name)
    return next((by_folded[n] for n in wanted if n in by_folded), None)


def _degrees(rows: list[list[str]], lon: int, lat: int) -> bool:
    """Return whether ``rows`` hold at least one point and every point is in degrees."""
    seen = False
    for row in rows:
        if len(row) <= max(lon, lat):
            continue
        try:
            x, y = float(row[lon]), float(row[lat])
        except ValueError:
            continue
        if not (-180 <= x <= 180 and -90 <= y <= 90):
            return False
        seen = True
    return seen
