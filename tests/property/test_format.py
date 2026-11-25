from __future__ import annotations

import math

from hypothesis import example, given
from hypothesis import strategies as st

from portolan_registry_qgis.core.format import (
    catalog_summary,
    excerpt,
    format_label,
    human_size,
    license_name,
    link_label,
)

SIZES = st.integers(0, 2**64 - 1)
EXTENSIONS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8)


def displayed_bytes(size: int) -> float:
    number, unit = human_size(size).split()
    power = ("B", "KB", "MB", "GB", "TB", "PB").index(unit)
    return float(number.replace(",", "")) * math.pow(1000, power)


@given(SIZES, SIZES)
@example(999, 1000)
@example(999999, 1000000)
@example(999999999, 1000000000)
def test_human_size_is_monotone_and_rounds_within_one_display_unit(first: int, second: int) -> None:
    # Contract: core.format.human_size writes a short decimal byte count.
    low, high = sorted((first, second))
    assert displayed_bytes(low) <= displayed_bytes(high)
    for size in (low, high):
        shown = displayed_bytes(size)
        tolerance = max(1, size * 0.051)
        assert math.isclose(shown, size, abs_tol=tolerance)


@given(st.one_of(st.none(), st.integers(-(2**64), -1)))
def test_unknown_size_has_no_label(size: int | None) -> None:
    # Contract: core.format.human_size returns an empty string for unknown sizes.
    assert not human_size(size)


@given(st.text(), st.integers(1, 500))
def test_excerpt_is_bounded_and_idempotent(text: str, limit: int) -> None:
    # Contract: core.format.excerpt returns at most limit characters on one line.
    result = excerpt(text, limit)
    assert len(result) <= limit
    assert result == " ".join(result.split())
    assert excerpt(result, limit) == result
    flat = " ".join(text.split())
    assert result == flat if len(flat) <= limit else result.endswith("…")


@given(EXTENSIONS, st.text(alphabet="abc012?=&", max_size=30))
def test_extension_and_document_labels_ignore_query(extension: str, query: str) -> None:
    # Contracts: core.format.format_label uses extensions and link_label uses document names.
    name = f"asset.{extension}"
    href = f"https://example.org/{name}?{query}"
    assert format_label(None, None, href) == extension.upper()
    assert link_label(href, None) == name


@given(media=st.text(), href=st.text(), tabular=st.booleans())
def test_known_formats_take_priority_over_other_metadata(
    media: str, href: str, *, tabular: bool
) -> None:
    # Contract: core.format.format_label uses a detected format before the other fields.
    assert format_label("parquet", media, href, tabular=tabular) == (
        "Parquet" if tabular else "GeoParquet"
    )


@given(st.text(), st.lists(st.text(), max_size=5), st.one_of(st.none(), SIZES))
def test_license_and_summary_formatting_are_total(
    license_value: str,
    licenses: list[str],
    size: int | None,
) -> None:
    # Contracts: core.format.license_name and catalog_summary return display strings.
    assert isinstance(license_name(license_value), str)
    assert isinstance(catalog_summary(len(licenses), size, tuple(licenses)), str)
