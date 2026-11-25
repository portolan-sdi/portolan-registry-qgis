"""Hypothesis strategies that the property tests share."""

from __future__ import annotations

import json
import string
from typing import TYPE_CHECKING

from hypothesis import strategies as st

from tests import stac_builders

if TYPE_CHECKING:
    import random
    from collections.abc import Callable

    # Writes a JSON value as text.
    Dumps = Callable[[object], str]

# Characters that RFC 3986 section 2.3 lists as unreserved.
UNRESERVED = string.ascii_letters + string.digits + "-._~"
# Text that tries to leave its SQL quotes.
SQL_HOSTILE = (
    "'",
    '"',
    ";",
    "?",
    "''",
    '""',
    "'; DROP TABLE t; --",
    '" OR 1=1 --',
    "a, b FROM c",
)
# Text that tries to add HTML markup.
MARKUP = ("<script>alert(1)</script>", "&lt;b&gt;", '"><br><a href="x">', "<b>x</b>")
EXTENSIONS = ("parquet", "pmtiles", "tif", "json", "png", "fgb", "csv", "geojson", "copc.laz")
MEDIA_TYPES = (
    None,
    "application/vnd.apache.parquet",
    "application/vnd.pmtiles",
    "image/tiff; application=geotiff; profile=cloud-optimized",
    "image/png",
    "application/vnd.mapbox.style+json",
    "application/json",
)
ROLES = ("data", "visual", "thumbnail", "overview", "metadata", "style", "default", "source")

segments = st.text(UNRESERVED, min_size=1, max_size=8).filter(lambda s: s not in {".", ".."})
hosts = st.lists(
    st.text(string.ascii_lowercase + string.digits, min_size=1, max_size=6), min_size=1, max_size=3
).map(lambda labels: ".".join([*labels, "test"]))
# Unicode scalar values except NUL. DuckDB strings cannot hold NUL, and a
# lone surrogate is not a Unicode character.
sql_characters = st.characters(codec="utf-8", exclude_characters="\x00")
hostile_text = st.one_of(st.sampled_from(SQL_HOSTILE), st.text(sql_characters))
markup = st.one_of(st.sampled_from(MARKUP), st.text(max_size=8))
names = st.text(sql_characters, min_size=1).filter(str.strip)
titles = st.one_of(st.none(), st.text(max_size=12))


def paths(min_size: int = 0) -> st.SearchStrategy[str]:
    """Return a strategy for a relative path of unreserved segments."""
    return st.lists(segments, min_size=min_size, max_size=3).map("/".join)


def _folder(scheme: str, host: str, path: str) -> str:
    return f"{scheme}://{host}/" + (f"{path}/" if path else "")


# An http(s) folder URL that ends with a slash.
folders = st.builds(_folder, st.sampled_from(("http", "https")), hosts, paths())
files = st.builds(lambda path, ext: f"{path}.{ext}", paths(1), st.sampled_from(EXTENSIONS))

json_numbers = st.one_of(
    st.integers(),
    st.integers(min_value=10**300, max_value=10**400),
    st.integers(min_value=-(10**400), max_value=-(10**300)),
    st.floats(),
)
json_scalars = st.one_of(st.none(), st.booleans(), json_numbers, st.text(max_size=12))
json_values: st.SearchStrategy[object] = st.recursive(
    json_scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=6), inner, max_size=4)
    ),
    max_leaves=12,
)


Box = tuple[float, float, float, float]


@st.composite
def boxes(draw: st.DrawFn, lon: st.SearchStrategy[float], lat: st.SearchStrategy[float]) -> Box:
    """Return a box with south at most north. West above east crosses the antimeridian."""
    south, north = sorted((draw(lat), draw(lat)))
    return draw(lon), south, draw(lon), north


@st.composite
def bboxes(draw: st.DrawFn, *, three_d: bool = False) -> list[float]:
    """Return a WGS84 STAC bbox, in 2D or 3D."""
    west, south, east, north = draw(boxes(st.floats(-180, 180), st.floats(-90, 90)))
    if not three_d:
        return [west, south, east, north]
    low, high = sorted((draw(st.floats(-1e4, 1e4)), draw(st.floats(-1e4, 1e4))))
    return [west, south, low, east, north, high]


@st.composite
def assets(draw: st.DrawFn) -> dict[str, dict[str, object]]:
    """Return a STAC assets object whose hrefs are relative files."""
    keys = draw(st.lists(st.text(min_size=1, max_size=8), unique=True, max_size=5))
    found: dict[str, dict[str, object]] = {}
    for key in keys:
        asset: dict[str, object] = {
            "href": draw(st.one_of(files, st.builds("./{}".format, files))),
            "roles": draw(st.lists(st.sampled_from(ROLES), unique=True, max_size=3)),
        }
        media = draw(st.sampled_from(MEDIA_TYPES))
        if media is not None:
            asset["type"] = media
        title = draw(titles)
        if title is not None:
            asset["title"] = title
        found[key] = asset
    return found


def _text_fields(draw: st.DrawFn) -> dict[str, object]:
    fields: dict[str, object] = {}
    for name in ("title", "description", "license"):
        value = draw(titles)
        if value is not None:
            fields[name] = value
    return fields


link_hrefs = st.one_of(
    files, st.builds("./{}/catalog.json".format, segments), st.builds("../{}.json".format, segments)
)


@st.composite
def documents(draw: st.DrawFn) -> dict[str, object]:
    """Return a catalog, collection, or item from the builders in ``tests.stac_builders``."""
    ident = draw(st.text(min_size=1, max_size=10))
    children = draw(st.lists(link_hrefs, max_size=3))
    other = stac_builders.links(
        draw(st.sampled_from(("pmtiles", "license", "via", "icon"))),
        draw(st.lists(link_hrefs, max_size=2)),
    )
    fields = _text_fields(draw)
    if draw(st.booleans()):
        fields["assets"] = draw(assets())
    kind = draw(st.sampled_from(("catalog", "collection", "item")))
    if kind == "catalog":
        return stac_builders.make_catalog(ident, children, other, **fields)
    if kind == "collection":
        extent = {"spatial": {"bbox": [draw(bboxes())]}, "temporal": {"interval": [[None, None]]}}
        return stac_builders.make_collection(ident, children, other, extent=extent, **fields)
    bbox = draw(st.one_of(st.none(), bboxes(), bboxes(three_d=True)))
    return stac_builders.make_item(ident, bbox, other, **fields)


@st.composite
def mutated(draw: st.DrawFn, base: st.SearchStrategy[object]) -> object:
    """Return a value of ``base`` with one to three values replaced by any JSON value."""
    value = draw(base)
    for _round in range(draw(st.integers(1, 3))):
        value = _replace_one(draw, value)
    return value


def _replace_one(draw: st.DrawFn, value: object) -> object:
    descend = draw(st.integers(0, 4)) > 0
    if descend and isinstance(value, dict) and value:
        key = draw(st.sampled_from(sorted(value)))
        return {**value, key: _replace_one(draw, value[key])}
    if descend and isinstance(value, list) and value:
        index = draw(st.integers(0, len(value) - 1))
        return [*value[:index], _replace_one(draw, value[index]), *value[index + 1 :]]
    return draw(json_values)


def shuffled(value: object, rnd: random.Random, *, skip: frozenset[str] = frozenset()) -> object:
    """Return ``value`` with the members of each object in a random order.

    An object under a key in ``skip`` keeps the order of its own members.
    """
    return _shuffled(value, rnd, skip, keep=False)


def _shuffled(value: object, rnd: random.Random, skip: frozenset[str], *, keep: bool) -> object:
    if isinstance(value, list):
        return [_shuffled(item, rnd, skip, keep=False) for item in value]
    if not isinstance(value, dict):
        return value
    items = [(key, _shuffled(item, rnd, skip, keep=key in skip)) for key, item in value.items()]
    if not keep:
        rnd.shuffle(items)
    return dict(items)


def escape_all(value: object) -> str:
    """Return ``value`` as JSON text that writes every string character as ``\\uXXXX``.

    RFC 8259 section 7 lets any character be escaped. A character outside the
    Basic Multilingual Plane becomes its UTF-16 surrogate pair.
    """
    if isinstance(value, str):
        units = value.encode("utf-16-be")
        pairs = (units[i : i + 2].hex() for i in range(0, len(units), 2))
        return '"' + "".join(f"\\u{pair}" for pair in pairs) + '"'
    if isinstance(value, list):
        return "[" + ",".join(escape_all(item) for item in value) + "]"
    if isinstance(value, dict):
        members = (f"{escape_all(key)}:{escape_all(item)}" for key, item in value.items())
        return "{" + ",".join(members) + "}"
    return json.dumps(value)


json_texts: st.SearchStrategy[Dumps] = st.sampled_from(
    (
        json.dumps,
        lambda value: json.dumps(value, indent=2, ensure_ascii=False),
        lambda value: json.dumps(value, indent="\t", sort_keys=True),
        lambda value: json.dumps(value, separators=(",", ":")),
        lambda value: " \r\n" + json.dumps(value, indent=0) + "\n\t ",
        escape_all,
    )
)
