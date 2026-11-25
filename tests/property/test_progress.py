from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core.progress import Clock, left_text, progress_text

FRACTIONS = st.floats(-10, 10, allow_nan=False, allow_infinity=False)
SECONDS = st.floats(0, 1e12, allow_nan=False, allow_infinity=False)


@given(st.lists(FRACTIONS, max_size=100))
def test_share_is_a_clamped_running_maximum(fractions: list[float]) -> None:
    # Contract: core.progress.Clock.share returns the largest share so far, from 0 to 1.
    clock = Clock()
    expected = 0.0
    for fraction in fractions:
        expected = max(expected, min(1, max(0, fraction)))
        assert clock.share(fraction) == expected


@given(st.integers(1, 20), st.integers(31, 99), st.integers(1, 10000), st.integers(1, 10000))
def test_remaining_time_matches_a_constant_rate(
    first_percent: int,
    percent: int,
    start: int,
    elapsed: int,
) -> None:
    # Contract: core.progress.Clock.left estimates seconds from the rate since the first report.
    clock = Clock()
    first, fraction = first_percent / 100, percent / 100
    assert clock.left(first, start) is None
    remaining = clock.left(fraction, start + elapsed)
    assert remaining == pytest.approx(elapsed * (1 - fraction) / (fraction - first))
    assert remaining is not None
    assert math.isfinite(remaining)
    assert remaining >= 0
    assert clock.left(1, start + elapsed) is None


@given(st.floats(0.01, 0.8), st.floats(0, 0.09), SECONDS)
def test_clock_waits_for_a_sufficient_gain(first: float, gain: float, elapsed: float) -> None:
    # Contract: core.progress.Clock.left waits for MIN_GAIN after its first report.
    clock = Clock()
    assert clock.left(first, 0) is None
    assert clock.left(first + gain, elapsed) is None


@given(st.text(), FRACTIONS, st.one_of(st.none(), SECONDS))
def test_progress_text_preserves_message_and_clamps_percent(
    message: str,
    fraction: float,
    remaining: float | None,
) -> None:
    # Contract: core.progress.progress_text appends a percentage and optional time left.
    result = progress_text(message, fraction, remaining)
    assert result.startswith(message + " ")
    suffix = result[len(message) + 1 :]
    percent, separator, tail = suffix.partition("%")
    assert separator == "%"
    assert int(percent) == max(0, min(100, int(fraction * 100)))
    assert tail == ("" if remaining is None else ", " + left_text(remaining))
    assert progress_text(message, None, remaining) == message


@given(SECONDS)
def test_time_label_is_total_and_rounded(seconds: float) -> None:
    # Contract: core.progress.left_text returns the time left in rounded words.
    label = left_text(seconds)
    prefix, number, unit, suffix = label.split()
    assert (prefix, suffix) == ("about", "left")
    assert int(number) > 0
    assert unit in {"s", "min", "hour", "hours"}
