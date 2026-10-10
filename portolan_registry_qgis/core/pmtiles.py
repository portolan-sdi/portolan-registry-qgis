"""Read PMTiles v3 archives through HTTP range requests.

The reader follows the PMTiles v3 specification
(https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md). It takes a
``fetch_range(offset, length)`` function, so the caller decides how bytes
travel. Inside QGIS that function uses QGIS's network stack, which applies the
user's proxy and authentication settings.
"""

from __future__ import annotations

import gzip
import json
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

HEADER_LENGTH = 127
# The specification recommends one 16 KiB first read. It holds the header
# and, in most archives, the whole root directory.
INITIAL_READ = 16384
TILE_TYPES = {0: "unknown", 1: "mvt", 2: "png", 3: "jpeg", 4: "webp", 5: "avif", 6: "mlt"}
COMPRESSIONS = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
_MAX_DEPTH = 4
_LEAF_CACHE = 64


class PmtilesError(OSError):
    """The bytes are not a PMTiles v3 archive this reader can decode."""


@dataclass(frozen=True)
class Header:
    """The fixed 127-byte PMTiles v3 header."""

    root_offset: int
    root_length: int
    metadata_offset: int
    metadata_length: int
    leaf_offset: int
    leaf_length: int
    data_offset: int
    data_length: int
    internal_compression: str
    tile_compression: str
    tile_type: str
    min_zoom: int
    max_zoom: int
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class Entry:
    """One directory entry. A run length of 0 marks a leaf directory."""

    tile_id: int
    offset: int
    length: int
    run_length: int


def parse_header(data: bytes) -> Header:
    """Decode the archive header.

    Raises:
        PmtilesError: The bytes do not start with a PMTiles v3 header.
    """
    if len(data) < HEADER_LENGTH or data[:7] != b"PMTiles":
        raise PmtilesError("Not a PMTiles archive")
    if data[7] != 3:
        raise PmtilesError(f"PMTiles version {data[7]} is not supported; version 3 is")
    numbers = struct.unpack_from("<11Q", data, 8)
    clustered_etc = struct.unpack_from("<BBBBBB", data, 96)
    west, south, east, north = struct.unpack_from("<iiii", data, 102)
    return Header(
        root_offset=numbers[0],
        root_length=numbers[1],
        metadata_offset=numbers[2],
        metadata_length=numbers[3],
        leaf_offset=numbers[4],
        leaf_length=numbers[5],
        data_offset=numbers[6],
        data_length=numbers[7],
        internal_compression=COMPRESSIONS.get(clustered_etc[1], "unknown"),
        tile_compression=COMPRESSIONS.get(clustered_etc[2], "unknown"),
        tile_type=TILE_TYPES.get(clustered_etc[3], "unknown"),
        min_zoom=clustered_etc[4],
        max_zoom=clustered_etc[5],
        bounds=(west / 1e7, south / 1e7, east / 1e7, north / 1e7),
    )


def decompress(data: bytes, compression: str) -> bytes:
    """Undo the archive's compression.

    Raises:
        PmtilesError: The compression is not gzip or none. Python's standard
            library has no brotli decoder, and zstd only from 3.14.
    """
    if compression in {"none", "unknown"}:
        return data
    if compression == "gzip":
        return gzip.decompress(data)
    raise PmtilesError(f"{compression} compression is not supported")


def _varint(data: bytes, position: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if position >= len(data):
            raise PmtilesError("Truncated directory")
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7


def parse_directory(data: bytes) -> list[Entry]:
    """Decode a decompressed directory into its entries."""
    count, position = _varint(data, 0)
    columns: list[list[int]] = []
    for _ in range(4):
        column = []
        for _ in range(count):
            value, position = _varint(data, position)
            column.append(value)
        columns.append(column)
    ids, runs, lengths, offsets = columns
    entries: list[Entry] = []
    tile_id = 0
    for index in range(count):
        tile_id += ids[index]
        if offsets[index] == 0 and index > 0:
            offset = entries[-1].offset + entries[-1].length
        else:
            offset = offsets[index] - 1
        entries.append(Entry(tile_id, offset, lengths[index], runs[index]))
    return entries


def _rotate(size: int, x: int, y: int, rx: int, ry: int) -> tuple[int, int]:
    if ry == 0:
        if rx != 0:
            x = size - 1 - x
            y = size - 1 - y
        return y, x
    return x, y


def zxy_to_tile_id(z: int, x: int, y: int) -> int:
    """Return the Hilbert tile id of a tile, as the specification defines it."""
    if z > 31 or not (0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise ValueError(f"Tile {z}/{x}/{y} is outside the tile pyramid")
    tile_id = ((1 << (z * 2)) - 1) // 3
    for level in range(z - 1, -1, -1):
        size = 1 << level
        rx = size & x
        ry = size & y
        tile_id += ((3 * rx) ^ ry) << level
        x, y = _rotate(size, x, y, rx, ry)
    return tile_id


def find_entry(entries: list[Entry], tile_id: int) -> Entry | None:
    """Return the entry that holds ``tile_id``, or the leaf that may hold it."""
    low, high = 0, len(entries) - 1
    while low <= high:
        middle = (low + high) // 2
        if entries[middle].tile_id < tile_id:
            low = middle + 1
        elif entries[middle].tile_id > tile_id:
            high = middle - 1
        else:
            return entries[middle]
    if high >= 0:
        entry = entries[high]
        if entry.run_length == 0 or tile_id - entry.tile_id < entry.run_length:
            return entry
    return None


class Reader:
    """Read the header, metadata, and tiles of one archive.

    The reader caches the root directory and recent leaf directories. It is
    safe to call from several threads, which is how the tile server uses it.
    """

    def __init__(self, fetch_range: Callable[[int, int], bytes]):
        self._fetch = fetch_range
        self._lock = threading.Lock()
        self._header: Header | None = None
        # The first read. Metadata that sits inside it needs no second request.
        self._first = b""
        self._root: list[Entry] | None = None
        self._leaves: OrderedDict[tuple[int, int], list[Entry]] = OrderedDict()

    def header(self) -> Header:
        """Return the header, reading it and the root directory on first use."""
        with self._lock:
            if self._header is None:
                first = self._fetch(0, INITIAL_READ)
                header = parse_header(first)
                end = header.root_offset + header.root_length
                raw = (
                    first[header.root_offset : end]
                    if end <= len(first)
                    else self._fetch(header.root_offset, header.root_length)
                )
                self._root = parse_directory(decompress(raw, header.internal_compression))
                self._first = first
                self._header = header
            return self._header

    def metadata(self) -> dict[str, Any]:
        """Return the archive's JSON metadata.

        Writers put the metadata after the root directory, so it is usually
        inside the first read and costs no request.
        """
        header = self.header()
        if header.metadata_length == 0:
            return {}
        end = header.metadata_offset + header.metadata_length
        raw = (
            self._first[header.metadata_offset : end]
            if end <= len(self._first)
            else self._fetch(header.metadata_offset, header.metadata_length)
        )
        value = json.loads(decompress(raw, header.internal_compression))
        return value if isinstance(value, dict) else {}

    def _leaf(self, header: Header, offset: int, length: int) -> list[Entry]:
        key = (offset, length)
        with self._lock:
            cached = self._leaves.get(key)
            if cached is not None:
                self._leaves.move_to_end(key)
                return cached
        raw = self._fetch(header.leaf_offset + offset, length)
        entries = parse_directory(decompress(raw, header.internal_compression))
        with self._lock:
            self._leaves[key] = entries
            while len(self._leaves) > _LEAF_CACHE:
                self._leaves.popitem(last=False)
        return entries

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        """Return one tile, decompressed, or None when the archive lacks it."""
        header = self.header()
        if not header.min_zoom <= z <= header.max_zoom:
            return None
        try:
            tile_id = zxy_to_tile_id(z, x, y)
        except ValueError:
            return None
        entries = self._root or []
        for _ in range(_MAX_DEPTH):
            entry = find_entry(entries, tile_id)
            if entry is None:
                return None
            if entry.run_length > 0:
                raw = self._fetch(header.data_offset + entry.offset, entry.length)
                return decompress(raw, header.tile_compression)
            entries = self._leaf(header, entry.offset, entry.length)
        raise PmtilesError("Directory nesting is deeper than the specification allows")
