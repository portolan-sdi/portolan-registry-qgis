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
    """Return the longitude and latitude column names of a CSV ``header``, or None.

    Columns x and y count only when every complete row holds degrees,
    because x and y are often projected.
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
    planar = bool({lon.casefold(), lat.casefold()} & _PLANAR_NAMES)
    if planar and not _degrees(_complete_rows(rows, text), names.index(lon), names.index(lat)):
        return None
    return lon, lat


def _complete_rows(rows: list[list[str]], text: str) -> list[list[str]]:
    """Return the rows after the header. The last line can stop in the middle of a row."""
    return rows[1:] if text.endswith(("\n", "\r")) else rows[1:-1]


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
