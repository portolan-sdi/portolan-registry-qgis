from __future__ import annotations

import datetime

import pytest

from scripts.release_changelog import ChangelogError, release

DATE = datetime.date(2026, 10, 9)
CHANGELOG = """# Changelog

## Unreleased

- A new thing.

## 0.1.0 - 2026-10-07

- The first thing.
"""


def test_release_moves_the_notes_under_the_version():
    assert release(CHANGELOG, "0.1.1", DATE) == (
        "# Changelog\n\n## Unreleased\n\n## 0.1.1 - 2026-10-09\n\n- A new thing.\n\n"
        "## 0.1.0 - 2026-10-07\n\n- The first thing.\n"
    )


def test_release_of_the_last_section():
    text = "# Changelog\n\n## Unreleased\n\n- Only thing.\n"
    assert release(text, "0.1.0", DATE).endswith("## 0.1.0 - 2026-10-09\n\n- Only thing.\n\n")


def test_empty_notes_stop_the_release():
    with pytest.raises(ChangelogError, match="Add the changes in 0.1.1"):
        release("# Changelog\n\n## Unreleased\n\n## 0.1.0 - 2026-10-07\n\n- x\n", "0.1.1", DATE)


def test_a_subheading_stops_the_release():
    with pytest.raises(ChangelogError, match="subheadings"):
        release("# Changelog\n\n## Unreleased\n\n### Added\n\n- x\n", "0.1.1", DATE)


def test_a_missing_unreleased_heading_stops_the_release():
    with pytest.raises(ChangelogError, match="no '## Unreleased'"):
        release("# Changelog\n\n## 0.1.0 - 2026-10-07\n\n- x\n", "0.1.1", DATE)
