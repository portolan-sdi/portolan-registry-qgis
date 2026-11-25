from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core.multihash import ChecksumError, decode, file_matches

HASHES = (
    (0x11, "sha1"),
    (0x12, "sha256"),
    (0x13, "sha512"),
    (0x14, "sha3_512"),
    (0x15, "sha3_384"),
    (0x16, "sha3_256"),
    (0x17, "sha3_224"),
    (0xB220, "blake2b"),
)


def unsigned_varint(value: int) -> bytes:
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


@pytest.mark.parametrize(("code", "algorithm"), HASHES)
@given(st.binary(max_size=4096), st.data())
def test_every_hash_verifies_and_rejects_a_changed_byte(
    code: int,
    algorithm: str,
    payload: bytes,
    data: st.DataObject,
) -> None:
    # Multihash spec, Format: https://github.com/multiformats/multihash#the-multihash-format
    digest = (
        hashlib.blake2b(payload, digest_size=32)
        if algorithm == "blake2b"
        else hashlib.new(algorithm, payload)
    ).digest()
    encoded = (unsigned_varint(code) + unsigned_varint(len(digest)) + digest).hex()
    decoded = decode(encoded)
    assert (decoded.algorithm, decoded.digest) == (algorithm, digest)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "payload.bin"
        path.write_bytes(payload)
        seen: list[int] = []
        assert file_matches(path, encoded, progress=seen.append)
        assert seen == ([len(payload)] if payload else [])
        changed = bytearray(payload or b"\x00")
        index = data.draw(st.integers(0, len(changed) - 1))
        changed[index] ^= 1
        path.write_bytes(changed)
        assert not file_matches(path, encoded)


@given(
    st.integers(0, 2**63 - 1).filter(lambda code: code not in dict(HASHES)), st.binary(max_size=64)
)
def test_unknown_codes_raise_checksum_error(code: int, digest: bytes) -> None:
    # Contract: core.multihash.decode raises ChecksumError for unsupported functions.
    value = unsigned_varint(code) + unsigned_varint(len(digest)) + digest
    with pytest.raises(ChecksumError, match="Unsupported"):
        decode(value.hex())


@given(st.sampled_from(HASHES), st.binary(max_size=64), st.integers(1, 128))
def test_digest_length_mismatch_raises(
    algorithm: tuple[int, str],
    digest: bytes,
    extra: int,
) -> None:
    # Multihash spec, Format: the digest length prefixes the digest bytes.
    value = unsigned_varint(algorithm[0]) + unsigned_varint(len(digest) + extra) + digest
    with pytest.raises(ChecksumError, match="Digest"):
        decode(value.hex())


@given(st.binary(max_size=128))
def test_arbitrary_multihashes_have_only_declared_decode_errors(raw: bytes) -> None:
    # Contract: core.multihash.decode raises ChecksumError for invalid encodings.
    try:
        result = decode(raw.hex())
    except ChecksumError:
        return
    assert result.algorithm in dict(HASHES).values()
