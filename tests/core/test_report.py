"""The URLs of the forms that report a dataset problem or a plugin bug."""

from __future__ import annotations

import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import portolan_registry_qgis
from portolan_registry_qgis.core.report import (
    DATASET_TEMPLATE,
    PLUGIN_TEMPLATE,
    Versions,
    bug_url,
    problem_url,
    read_metadata,
)

VERSIONS = Versions(
    plugin="0.2.0",
    qgis="3.40.1-Bratislava",
    qt="5.15.13",
    python="3.12.3",
    gdal="3.9.2",
    duckdb="1.5.0",
    os="Linux-6.8.0-x86_64-with-glibc2.39",
)
TODAY = datetime.date(2026, 10, 19)


def fields(url: str) -> dict[str, str]:
    query = parse_qs(urlsplit(url).query, keep_blank_values=True, strict_parsing=True)
    assert all(len(values) == 1 for values in query.values())
    return {key: values[0] for key, values in query.items()}


def test_problem_url_keeps_spaces_and_non_ascii_characters():
    href = "https://data.example/São Paulo/ruas é/collection.json"
    link = (
        "https://browser.portolan-sdi.org/#/external/data.example/São Paulo/ruas é/collection.json"
    )
    url = problem_url("pergamino-ide", href, link, TODAY, VERSIONS)
    assert url.startswith("https://github.com/portolan-sdi/portolan-registry/issues/new?")
    # The query has no raw space, plus sign, or non-ASCII character.
    query = urlsplit(url).query
    assert query.isascii()
    assert " " not in query
    assert "+" not in query
    assert fields(url) == {
        "template": DATASET_TEMPLATE,
        "catalog-id": "pergamino-ide",
        "dataset": f"STAC document: {href}\nBrowser page: {link}",
        "observed": "2026-10-19",
        "tool": "Portolan Registry QGIS plugin 0.2.0, QGIS 3.40.1-Bratislava",
    }


def test_problem_url_without_a_browser_page():
    url = problem_url("test", "file:///tmp/collection.json", None, TODAY, VERSIONS)
    assert fields(url)["dataset"] == "STAC document: file:///tmp/collection.json"


def test_bug_url_fills_the_version():
    url = bug_url("https://github.com/portolan-sdi/portolan-registry-qgis/issues/", VERSIONS)
    assert url.startswith("https://github.com/portolan-sdi/portolan-registry-qgis/issues/new?")
    assert fields(url) == {
        "template": PLUGIN_TEMPLATE,
        "version": (
            "Portolan Registry 0.2.0; QGIS 3.40.1-Bratislava; Qt 5.15.13; Python 3.12.3; "
            "GDAL 3.9.2; DuckDB 1.5.0; OS Linux-6.8.0-x86_64-with-glibc2.39"
        ),
    }


def test_bug_url_when_duckdb_is_not_installed():
    versions = Versions(**{**VERSIONS.__dict__, "duckdb": None})
    version = fields(bug_url("https://tracker.test/issues", versions))["version"]
    assert "; DuckDB not installed; " in version


def test_read_metadata_of_the_plugin():
    path = Path(portolan_registry_qgis.__file__).with_name("metadata.txt")
    version, tracker = read_metadata(path)
    assert version.count(".") == 2
    assert tracker == "https://github.com/portolan-sdi/portolan-registry-qgis/issues"


def test_read_metadata_keeps_percent_signs_and_colons(tmp_path):
    path = tmp_path / "metadata.txt"
    path.write_text(
        "[general]\nabout=100% cloud-native: file:checksum\nversion=1.2.3\n"
        "tracker=https://tracker.test/issues\n",
        encoding="utf-8",
    )
    assert read_metadata(path) == ("1.2.3", "https://tracker.test/issues")
