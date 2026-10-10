"""The PMTiles v3 reader, checked against archives the reference writer builds."""

from __future__ import annotations

import gzip
import json
import random

import pytest
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import write

from portolan_registry_qgis.core.pmtiles import (
    PmtilesError,
    Reader,
    find_entry,
    parse_header,
    zxy_to_tile_id,
)


def file_fetcher(path, log=None):
    def fetch(offset, length):
        if log is not None:
            log.append((offset, length))
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    return fetch


def build(path, tiles, compression=Compression.GZIP, tile_type=TileType.MVT, metadata=None):
    with write(str(path)) as writer:
        for (z, x, y), data in sorted(tiles.items(), key=lambda t: zxy_to_tileid(*t[0])):
            payload = gzip.compress(data, mtime=0) if compression == Compression.GZIP else data
            writer.write_tile(zxy_to_tileid(z, x, y), payload)
        writer.finalize(
            {
                "tile_type": tile_type,
                "tile_compression": compression,
                "min_lon_e7": int(-75.28 * 1e7),
                "min_lat_e7": int(39.86 * 1e7),
                "max_lon_e7": int(-74.95 * 1e7),
                "max_lat_e7": int(40.14 * 1e7),
                "center_zoom": 0,
                "center_lon_e7": 0,
                "center_lat_e7": 0,
            },
            {
                "vector_layers": [{"id": "land_use", "fields": {}}],
                "name": "test",
                **(metadata or {}),
            },
        )


@pytest.mark.parametrize("z", range(13))
def test_tile_ids_match_the_reference(z):
    rng = random.Random(z)
    for _ in range(50):
        x, y = rng.randrange(1 << z), rng.randrange(1 << z)
        assert zxy_to_tile_id(z, x, y) == zxy_to_tileid(z, x, y)


def test_tile_ids_known_values():
    assert [
        zxy_to_tile_id(*t) for t in [(0, 0, 0), (1, 0, 0), (1, 0, 1), (1, 1, 1), (1, 1, 0)]
    ] == [
        0,
        1,
        2,
        3,
        4,
    ]
    with pytest.raises(ValueError, match="outside"):
        zxy_to_tile_id(1, 2, 0)


def test_small_archive(tmp_path):
    path = tmp_path / "a.pmtiles"
    tiles = {(0, 0, 0): b"world", (1, 0, 1): b"south-west", (2, 3, 3): b"corner"}
    build(path, tiles)
    log = []
    reader = Reader(file_fetcher(path, log))
    header = reader.header()
    assert header.tile_type == "mvt"
    assert header.tile_compression == "gzip"
    assert (header.min_zoom, header.max_zoom) == (0, 2)
    assert header.bounds == pytest.approx((-75.28, 39.86, -74.95, 40.14))
    assert reader.metadata()["vector_layers"][0]["id"] == "land_use"
    # The metadata comes out of the first read, with no request of its own.
    assert log == [(0, 16384)]
    for (z, x, y), data in tiles.items():
        assert reader.tile(z, x, y) == data
    assert reader.tile(1, 1, 1) is None
    assert reader.tile(5, 0, 0) is None
    assert reader.tile(1, 9, 9) is None
    # One 16 KiB read covers the header and the root directory.
    assert log[0] == (0, 16384)


def test_leaf_directories_and_runs(tmp_path):
    path = tmp_path / "big.pmtiles"
    rng = random.Random(7)
    z = 12
    coords = {(z, rng.randrange(4096), rng.randrange(4096)) for _ in range(30000)}
    tiles = {c: f"tile {c}".encode() for c in coords}
    # Equal neighbors share bytes, which the writer stores as one run.
    tiles.update({(3, x, 0): b"same" for x in range(8)})
    build(path, tiles)
    reader = Reader(file_fetcher(path))
    header = reader.header()
    assert header.leaf_length > 0, "the fixture must exercise leaf directories"
    sample = rng.sample(sorted(coords), 300) + [(3, x, 0) for x in range(8)]
    for coord in sample:
        assert reader.tile(*coord) == tiles[coord]
    assert reader.tile(z, 0, 0) == tiles.get((z, 0, 0))


def test_uncompressed_tiles(tmp_path):
    path = tmp_path / "raw.pmtiles"
    build(path, {(0, 0, 0): b"raw"}, compression=Compression.NONE)
    assert Reader(file_fetcher(path)).tile(0, 0, 0) == b"raw"


def test_unsupported_compression(tmp_path):
    path = tmp_path / "br.pmtiles"
    build(path, {(0, 0, 0): b"x"}, compression=Compression.BROTLI)
    with pytest.raises(PmtilesError, match="brotli"):
        Reader(file_fetcher(path)).tile(0, 0, 0)


def test_raster_archive_reports_its_type(tmp_path):
    path = tmp_path / "png.pmtiles"
    build(path, {(0, 0, 0): b"png"}, tile_type=TileType.PNG)
    assert Reader(file_fetcher(path)).header().tile_type == "png"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"nope" * 40, "Not a PMTiles"),
        (b"PMTiles\x02" + bytes(120), "version 2"),
        (b"PMTiles", "Not a PMTiles"),
    ],
)
def test_bad_headers(data, message):
    with pytest.raises(PmtilesError, match=message):
        parse_header(data)


def test_find_entry_edges():
    assert find_entry([], 5) is None


def test_metadata_absent(tmp_path):
    path = tmp_path / "a.pmtiles"
    build(path, {(0, 0, 0): b"w"})
    raw = bytearray(path.read_bytes())
    # Zero the metadata length field (bytes 32-39).
    raw[32:40] = bytes(8)
    path.write_bytes(bytes(raw))
    assert Reader(file_fetcher(path)).metadata() == {}


def test_json_metadata_round_trip(tmp_path):
    path = tmp_path / "a.pmtiles"
    build(path, {(0, 0, 0): b"w"})
    assert json.dumps(Reader(file_fetcher(path)).metadata(), sort_keys=True).startswith("{")


def test_metadata_past_the_first_read(tmp_path):
    path = tmp_path / "a.pmtiles"
    # Random hex does not compress, so the metadata ends past 16 KiB.
    noise = random.Random(3).randbytes(24000).hex()
    build(path, {(0, 0, 0): b"w"}, metadata={"description": noise})
    log = []
    reader = Reader(file_fetcher(path, log))
    assert reader.metadata()["description"] == noise
    header = reader.header()
    assert log == [(0, 16384), (header.metadata_offset, header.metadata_length)]
