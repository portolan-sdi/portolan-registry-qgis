"""Build the GitHub issue form URLs that report a dataset problem or a plugin bug.

The forms are in English, so the text the plugin fills in is in English too.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

if TYPE_CHECKING:
    import datetime
    from pathlib import Path

# The registry mails the submitter of a catalog when this form is filed.
DATASET_FORM = "https://github.com/portolan-sdi/portolan-registry/issues/new"  # i18n: skip - a URL
# portolan-ops owns both forms and syncs them to their repos.
DATASET_TEMPLATE = "dataset-problem.yml"  # i18n: skip - a file name
PLUGIN_TEMPLATE = "plugin-problem.yml"  # i18n: skip - a file name


@dataclass(frozen=True)
class Versions:
    """The versions of the plugin and of the software that runs it."""

    plugin: str
    qgis: str
    qt: str
    python: str
    gdal: str
    duckdb: str | None
    os: str

    def tool(self) -> str:
        """Return the plugin and QGIS versions, for the "Tool" field."""
        return f"Portolan Registry QGIS plugin {self.plugin}, QGIS {self.qgis}"  # i18n: skip - the form is in English

    def line(self) -> str:
        """Return every version on one line, for the "Version" field."""
        duckdb = self.duckdb or "not installed"  # i18n: skip - the form is in English
        return "; ".join(
            (
                f"Portolan Registry {self.plugin}",
                f"QGIS {self.qgis}",
                f"Qt {self.qt}",
                f"Python {self.python}",
                f"GDAL {self.gdal}",
                f"DuckDB {duckdb}",
                f"OS {self.os}",
            )
        )


def read_metadata(path: Path) -> tuple[str, str]:
    """Return the version and the tracker URL from the plugin's ``metadata.txt``."""
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.read_string(path.read_text(encoding="utf-8"))
    general = parser["general"]
    return general["version"], general["tracker"]


def _query(fields: dict[str, str]) -> str:
    return urlencode(fields, quote_via=quote)


def problem_url(
    catalog_id: str,
    href: str,
    link: str | None,
    today: datetime.date,
    versions: Versions,
) -> str:
    """Return the registry's dataset problem form, filled for the document at ``href``.

    ``link`` is the browser page of the document, or None when it has none.
    """
    found = [f"STAC document: {href}"]  # i18n: skip - the form is in English
    if link:
        found.append(f"Browser page: {link}")  # i18n: skip - the form is in English
    fields = {
        "template": DATASET_TEMPLATE,
        "catalog-id": catalog_id,
        "dataset": "\n".join(found),
        "observed": today.isoformat(),
        "tool": versions.tool(),
    }
    return f"{DATASET_FORM}?{_query(fields)}"


def bug_url(tracker: str, versions: Versions) -> str:
    """Return the plugin problem form of the issue tracker at ``tracker``, with the Version filled."""
    fields = {"template": PLUGIN_TEMPLATE, "version": versions.line()}
    return f"{tracker.rstrip('/')}/new?{_query(fields)}"
