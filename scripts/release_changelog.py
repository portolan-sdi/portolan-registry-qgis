#!/usr/bin/env python3
"""Move the Unreleased notes in CHANGELOG.md under a new version heading.

`cz bump` runs this as a pre-bump hook and passes the new version in
CZ_PRE_NEW_VERSION. The hook edits CHANGELOG.md before the bump commit, and
`cz bump` commits it with the version files.

The heading has the form `## 0.1.1 - 2026-10-09`. qgis-plugin-ci reads that
form and copies the notes into the changelog that the QGIS plugin manager
shows. It ends a version's notes at the next `##`, so a `###` subheading would
cut them off. The notes are a flat list for that reason.

The script stops the bump when the notes are empty or contain a subheading.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
UNRELEASED = "## Unreleased"
_SECTION = re.compile(r"^## Unreleased[ \t]*\n(?P<notes>.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


class ChangelogError(Exception):
    """CHANGELOG.md cannot take a release."""


def release(text: str, version: str, date: datetime.date) -> str:
    """Return the changelog with the Unreleased notes under `version`."""
    match = _SECTION.search(text)
    if match is None:
        raise ChangelogError(f"CHANGELOG.md has no '{UNRELEASED}' heading.")
    notes = match.group("notes").strip()
    if not notes:
        raise ChangelogError(f"Add the changes in {version} under '{UNRELEASED}' in CHANGELOG.md.")
    if re.search(r"^#", notes, re.MULTILINE):
        raise ChangelogError(
            f"Remove the subheadings under '{UNRELEASED}'. qgis-plugin-ci reads only a flat list."
        )
    section = f"{UNRELEASED}\n\n## {version} - {date.isoformat()}\n\n{notes}\n\n"
    return text[: match.start()] + section + text[match.end() :].lstrip("\n")


def main() -> int:
    """Run the changelog step of a release."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "version",
        nargs="?",
        default=os.environ.get("CZ_PRE_NEW_VERSION"),
        help="The new version. Defaults to CZ_PRE_NEW_VERSION, which cz bump sets.",
    )
    args = parser.parse_args()
    if not args.version:
        parser.error("give the version, or run this through cz bump")
    try:
        text = release(
            CHANGELOG.read_text(encoding="utf-8"),
            args.version,
            datetime.datetime.now(datetime.UTC).date(),
        )
    except ChangelogError as error:
        print(f"{error}\nThe bump stopped. Run `git checkout -- .` to undo it.", file=sys.stderr)
        return 1
    CHANGELOG.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
