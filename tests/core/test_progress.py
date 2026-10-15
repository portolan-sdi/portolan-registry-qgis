from __future__ import annotations

import pytest

from portolan_registry_qgis.core.progress import Clock, left_text, progress_text


def test_the_clock_waits_for_a_gain_after_the_first_report():
    clock = Clock()
    assert clock.left(0.05, 1.0) is None
    # A stall at the first share gives no estimate, however long it lasts.
    assert clock.left(0.05, 6.0) is None
    assert clock.left(0.10, 7.0) is None
    assert clock.left(0.55, 6.0) == pytest.approx(5.0 * 0.45 / 0.5)
    assert clock.left(1.0, 9.0) is None
    assert Clock().left(0.0, 3.0) is None


def test_the_share_never_goes_back():
    clock = Clock()
    assert clock.share(0.7) == 0.7
    assert clock.share(0.0) == 0.7
    assert clock.share(1.5) == 1.0


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (1, "about 5 s left"),
        (27, "about 25 s left"),
        (59, "about 60 s left"),
        (150, "about 2 min left"),
        (3000, "about 50 min left"),
        (3700, "about 1 hour left"),
        (9000, "about 2 hours left"),
    ],
)
def test_left_text_rounds(seconds, text):
    assert left_text(seconds) == text


def test_progress_text():
    assert progress_text("Copying", None) == "Copying"
    assert progress_text("Copying", 0.02) == "Copying 2%"
    assert progress_text("Copying", 0.6, 10.0) == "Copying 60%, about 10 s left"
    assert progress_text("Copying", 1.4) == "Copying 100%"
