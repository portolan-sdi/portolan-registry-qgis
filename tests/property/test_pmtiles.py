from __future__ import annotations

import gzip
import io
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pmtiles.tile import (
    Compression,
    TileType,
    serialize_directory,
    serialize_header,
    tileid_to_zxy,
    write_varint,
    zxy_to_tileid,
)
from pmtiles.tile import (
    Entry as ReferenceEntry,
)

from portolan_registry_qgis.core.pmtiles import (
    Entry,
    Header,
    PmtilesError,
    find_entry,
    parse_directory,
    parse_header,
    zxy_to_tile_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from hypothesis.strategies import DrawFn
    from pmtiles.tile import HeaderDict


@st.composite
def headers(draw: DrawFn) -> HeaderDict:
    numbers = draw(st.lists(st.integers(0, 2**64 - 1), min_size=11, max_size=11))
    west, east = sorted(
        draw(st.tuples(st.integers(-1800000000, 1800000000), st.integers(-1800000000, 1800000000)))
    )
    south, north = sorted(
        draw(st.tuples(st.integers(-900000000, 900000000), st.integers(-900000000, 900000000)))
    )
    low, high = sorted(draw(st.tuples(st.integers(0, 31), st.integers(0, 31))))
    return {
        "version": 3,
        "root_offset": numbers[0],
        "root_length": numbers[1],
        "metadata_offset": numbers[2],
        "metadata_length": numbers[3],
        "leaf_directory_offset": numbers[4],
        "leaf_directory_length": numbers[5],
        "tile_data_offset": numbers[6],
        "tile_data_length": numbers[7],
        "addressed_tiles_count": numbers[8],
        "tile_entries_count": numbers[9],
        "tile_contents_count": numbers[10],
        "clustered": draw(st.booleans()),
        "internal_compression": draw(st.sampled_from(Compression)),
        "tile_compression": draw(st.sampled_from(Compression)),
        "tile_type": draw(st.sampled_from(TileType)),
        "min_zoom": low,
        "max_zoom": high,
        "min_lon_e7": west,
        "min_lat_e7": south,
        "max_lon_e7": east,
        "max_lat_e7": north,
        "center_zoom": low,
        "center_lon_e7": (west + east) // 2,
        "center_lat_e7": (south + north) // 2,
    }


@st.composite
def directories(draw: DrawFn) -> list[ReferenceEntry]:
    ids = sorted(draw(st.sets(st.integers(0, 2**64 - 1), max_size=30)))
    entries: list[ReferenceEntry] = []
    for tile_id in ids:
        contiguous = entries[-1].offset + entries[-1].length if entries else 0
        offset = draw(st.one_of(st.just(contiguous), st.integers(0, 2**63 - 1)))
        length = draw(st.integers(1, 2**32 - 1))
        run = draw(st.integers(0, 1000))
        entries.append(ReferenceEntry(tile_id, offset, length, run))
    return entries


@given(headers())
def test_header_matches_reference_writer(header: HeaderDict) -> None:
    # PMTiles v3 spec, Header: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md#header
    serialize = cast("Callable[[HeaderDict], bytes]", serialize_header)
    actual = parse_header(serialize(header))
    assert actual == Header(
        header["root_offset"],
        header["root_length"],
        header["metadata_offset"],
        header["metadata_length"],
        header["leaf_directory_offset"],
        header["leaf_directory_length"],
        header["tile_data_offset"],
        header["internal_compression"].name.lower(),
        header["tile_compression"].name.lower(),
        header["tile_type"].name.lower(),
        header["min_zoom"],
        header["max_zoom"],
        (
            header["min_lon_e7"] / 1e7,
            header["min_lat_e7"] / 1e7,
            header["max_lon_e7"] / 1e7,
            header["max_lat_e7"] / 1e7,
        ),
    )


@given(headers(), st.integers(0, 126), st.integers(0, 255).filter(lambda v: v != 3))
def test_truncated_and_wrong_version_headers_raise_declared_error(
    header: HeaderDict,
    cut: int,
    version: int,
) -> None:
    # Contract: core.pmtiles.parse_header raises PmtilesError for invalid headers.
    serialize = cast("Callable[[HeaderDict], bytes]", serialize_header)
    raw = serialize(header)
    with pytest.raises(PmtilesError):
        parse_header(raw[:cut])
    with pytest.raises(PmtilesError):
        parse_header(raw[:7] + bytes([version]) + raw[8:])


@given(st.binary(max_size=512))
def test_arbitrary_header_bytes_have_only_declared_errors(raw: bytes) -> None:
    # Contract: core.pmtiles.parse_header raises PmtilesError for invalid headers.
    try:
        header = parse_header(raw)
    except PmtilesError:
        return
    assert raw[:8] == b"PMTiles\x03"
    assert len(raw) >= 127
    assert isinstance(header, Header)


@given(directories())
def test_directory_matches_reference_writer(entries: list[ReferenceEntry]) -> None:
    # PMTiles v3 spec, Directories: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md#directories
    raw = gzip.decompress(serialize_directory(entries))
    parsed = parse_directory(raw)
    assert parsed == [Entry(e.tile_id, e.offset, e.length, e.run_length) for e in entries]
    for entry in parsed:
        assert find_entry(parsed, entry.tile_id) == entry


@given(directories().filter(bool), st.data())
def test_every_incomplete_directory_prefix_raises(
    entries: list[ReferenceEntry],
    data: st.DataObject,
) -> None:
    # Contract: core.pmtiles._varint reports a truncated directory as PmtilesError.
    raw = gzip.decompress(serialize_directory(entries))
    cut = data.draw(st.integers(0, len(raw) - 1))
    with pytest.raises(PmtilesError):
        parse_directory(raw[:cut])


@given(st.binary(max_size=256))
def test_corrupt_directories_have_only_declared_errors(raw: bytes) -> None:
    # Contract: core.pmtiles.parse_directory decodes entries or raises PmtilesError.
    try:
        parsed = parse_directory(raw)
    except PmtilesError:
        return
    assert all(isinstance(entry, Entry) for entry in parsed)


@given(st.integers(0, 2**64 - 1), st.integers(0, 2**64 - 1))
@example(127, 128)
@example(16383, 16384)
@example(2**64 - 1, 2**64 - 1)
def test_varint_boundaries_through_one_entry_directory(tile_id: int, length: int) -> None:
    # PMTiles v3 spec, Directories / Varint encoding (same spec URL as above).
    stream = io.BytesIO()
    encode = cast("Callable[[io.BytesIO, int], None]", write_varint)
    for value in (1, tile_id, 1, length, 1):
        encode(stream, value)
    assert parse_directory(stream.getvalue()) == [Entry(tile_id, 0, length, 1)]


@given(st.integers(0, 31), st.data())
def test_hilbert_id_round_trip(zoom: int, data: st.DataObject) -> None:
    # PMTiles v3 spec, TileID: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md#tileid
    x = data.draw(st.integers(0, (1 << zoom) - 1))
    y = data.draw(st.integers(0, (1 << zoom) - 1))
    tile_id = zxy_to_tile_id(zoom, x, y)
    assert tile_id == zxy_to_tileid(zoom, x, y)
    assert tileid_to_zxy(tile_id) == (zoom, x, y)
