from __future__ import annotations

import hashlib

import pytest

from portolan_registry_qgis.core.multihash import ChecksumError, decode, file_matches

DATA = b"portolan" * 1000


def sha256_multihash(data):
    return "1220" + hashlib.sha256(data).hexdigest()


def test_sha256_round_trip(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(DATA)
    assert file_matches(path, sha256_multihash(DATA))
    assert not file_matches(path, sha256_multihash(b"other"))


def test_other_functions(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(DATA)
    assert file_matches(path, "1340" + hashlib.sha512(DATA).hexdigest())
    assert file_matches(path, "1620" + hashlib.sha3_256(DATA).hexdigest())
    # blake2b-256 has the two-byte varint code 0xb220.
    blake = hashlib.blake2b(DATA, digest_size=32).hexdigest()
    assert file_matches(path, "a0e40220" + blake)


def test_progress_and_cancel(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(DATA)
    seen: list[int] = []
    assert file_matches(path, sha256_multihash(DATA), progress=seen.append)
    assert seen[-1] == len(DATA)
    assert not file_matches(path, sha256_multihash(DATA), cancelled=lambda: True)


def test_real_catalog_checksum_decodes():
    # file:checksum of the ANNCSU style asset, as the catalog publishes it.
    value = "12204da33adfa626c2f0e1b43ab40620c5bac5c5a438c0f60b2fde69d0727844b045"
    decoded = decode(value)
    assert decoded.algorithm == "sha256"
    assert len(decoded.digest) == 32


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("zz", "Not a hex"),
        ("12", "Truncated"),
        ("1220abcd", "Digest is 2 bytes"),
        ("0101ab", "Unsupported"),
        ("ffffffffffffffffffff01", "oversized"),
    ],
)
def test_bad_values(value, message):
    with pytest.raises(ChecksumError, match=message):
        decode(value)
