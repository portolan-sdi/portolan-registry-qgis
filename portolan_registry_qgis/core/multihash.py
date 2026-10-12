"""Verify downloaded files against a STAC ``file:checksum``.

The file extension stores checksums as hex-encoded multihash: a varint hash
function code, a varint digest length, then the digest. Portolan requires that
encoding (``specs/portolan/core.md``), and most catalogs use sha2-256, which
appears as the ``1220`` prefix.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Multihash codes from the multicodec table that hashlib can compute.
_ALGORITHMS: dict[int, str] = {
    0x11: "sha1",
    0x12: "sha256",
    0x13: "sha512",
    0x14: "sha3_512",
    0x15: "sha3_384",
    0x16: "sha3_256",
    0x17: "sha3_224",
    0xB220: "blake2b",
}
_CHUNK = 1024 * 1024


class Hasher(Protocol):
    """The part of a hashlib object this module uses."""

    def update(self, data: bytes, /) -> None:
        """Feed bytes to the hash."""

    def digest(self) -> bytes:
        """Return the digest of the bytes fed so far."""


# No person reads the text of a ChecksumError, so it is not translated. The
# downloader treats the error as an unreadable checksum and keeps the file.
class ChecksumError(ValueError):
    """A ``file:checksum`` value is not a multihash this module can verify."""


@dataclass(frozen=True)
class Multihash:
    """A decoded multihash."""

    algorithm: str
    digest: bytes

    def hasher(self) -> Hasher:
        """Return a fresh hashlib object for this multihash's function."""
        if self.algorithm == "blake2b":
            return hashlib.blake2b(digest_size=len(self.digest))
        return hashlib.new(self.algorithm)


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ChecksumError(
        "Truncated or oversized varint in multihash"  # i18n: skip
    )


def decode(value: str) -> Multihash:
    """Decode a hex-encoded multihash.

    Raises:
        ChecksumError: The value is not hex, is truncated, or names a hash
            function hashlib cannot compute.
    """
    try:
        data = bytes.fromhex(value.strip())
    except ValueError as error:
        raise ChecksumError(
            f"Not a hex string: {value!r}"  # i18n: skip
        ) from error
    code, offset = _varint(data, 0)
    length, offset = _varint(data, offset)
    digest = data[offset:]
    if len(digest) != length:
        raise ChecksumError(
            f"Digest is {len(digest)} bytes, but the multihash says {length}"  # i18n: skip
        )
    algorithm = _ALGORITHMS.get(code)
    if algorithm is None:
        raise ChecksumError(
            f"Unsupported multihash function 0x{code:x}"  # i18n: skip
        )
    return Multihash(algorithm, digest)


def file_matches(
    path: Path,
    checksum: str,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Return whether the file at ``path`` hashes to ``checksum``.

    Args:
        path: The file to hash.
        checksum: The asset's ``file:checksum``.
        progress: Called with the number of bytes read so far.
        cancelled: Polled between chunks. Returning True stops the hash and
            the function returns False.
    """
    expected = decode(checksum)
    hasher = expected.hasher()
    read = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            if cancelled is not None and cancelled():
                return False
            hasher.update(chunk)
            read += len(chunk)
            if progress is not None:
                progress(read)
    return hasher.digest() == expected.digest
