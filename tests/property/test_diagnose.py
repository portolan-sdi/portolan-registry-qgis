"""Property tests for the explanation of an asset that QGIS could not open."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import diagnose

# Text on one line, as GDAL writes one message.
one_line = st.text(st.characters(exclude_categories=("Cc", "Zl", "Zp")), min_size=1).filter(
    lambda line: line.strip() == line and len(line.splitlines()) == 1
)
messages = one_line.filter(lambda line: not line.startswith("Provider is not valid"))
uris = st.text(st.characters(exclude_categories=("Cc", "Zs", "Zl", "Zp")), min_size=1)


@given(st.lists(st.tuples(uris, messages), min_size=1, max_size=3))
def test_gdal_reason_keeps_each_message_without_its_prefix(found: list[tuple[str, str]]) -> None:
    detail = "\n".join(
        line
        for uri, message in found
        for line in (f"Cannot open GDAL dataset {uri}: {message}", "Provider is not valid (x)")
    )
    assert diagnose.gdal_reason(detail) == " ".join(message for _uri, message in found)


@given(st.text())
def test_gdal_reason_is_one_line(detail: str) -> None:
    assert len(diagnose.gdal_reason(detail).splitlines()) <= 1


@given(
    href=st.text(),
    codec=st.from_regex(r"\w+", fullmatch=True),
    flatpak=st.booleans(),
    worded=st.booleans(),
)
def test_a_missing_codec_is_named_in_the_advice(
    href: str, codec: str, *, flatpak: bool, worded: bool
) -> None:
    detail = (
        f"missing codec {codec}" if worded else f"{codec} compression support is not configured"
    )
    message = diagnose.open_failure(href, detail, flatpak=flatpak)
    assert href in message
    assert codec.upper() in message
    assert ("The QGIS Flatpak builds GDAL without" in message) == flatpak


@given(st.text(), st.text())
def test_open_failure_names_the_href(href: str, detail: str) -> None:
    assert href in diagnose.open_failure(href, detail, flatpak=False)
