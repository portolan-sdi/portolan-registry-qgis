"""Property tests for the issue form URLs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import report

if TYPE_CHECKING:
    import datetime as dt

versions = st.builds(
    report.Versions,
    plugin=st.text(),
    qgis=st.text(),
    qt=st.text(),
    python=st.text(),
    gdal=st.text(),
    duckdb=st.one_of(st.none(), st.text()),
    os=st.text(),
)


def _fields(url: str) -> dict[str, str]:
    """Return the query fields of ``url``, each of which must appear once."""
    parsed = parse_qs(urlsplit(url).query, keep_blank_values=True, strict_parsing=True)
    assert all(len(values) == 1 for values in parsed.values())
    return {name: values[0] for name, values in parsed.items()}


@given(st.text(), st.text(), st.one_of(st.none(), st.text()), st.dates(), versions)
def test_problem_url_carries_every_field_unchanged(
    catalog_id: str, href: str, link: str | None, today: dt.date, found: report.Versions
) -> None:
    url = report.problem_url(catalog_id, href, link, today, found)
    assert url.startswith(report.DATASET_FORM + "?")
    fields = _fields(url)
    assert fields["template"] == report.DATASET_TEMPLATE
    assert fields["catalog-id"] == catalog_id
    assert fields["observed"] == today.isoformat()
    assert fields["tool"] == found.tool()
    found_at = [f"STAC document: {href}", *([f"Browser page: {link}"] if link else [])]
    assert fields["dataset"] == "\n".join(found_at)


@given(st.text(), versions)
def test_bug_url_carries_the_versions_unchanged(tracker: str, found: report.Versions) -> None:
    url = report.bug_url(tracker, found)
    assert url.startswith(tracker.rstrip("/") + "/new?")
    assert _fields(url.removeprefix(tracker.rstrip("/"))) == {
        "template": report.PLUGIN_TEMPLATE,
        "version": found.line(),
    }
