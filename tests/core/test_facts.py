from __future__ import annotations

from html import escape

from portolan_registry_qgis.core.facts import BROWSER_LINK, facts_html, license_cell
from portolan_registry_qgis.core.stac import read_document

BASE = "https://data.test/points"
TERMS = "https://metadata.phila.gov/#help/help-faqs/what-are-the-terms-of-use/"
PORTAL = "https://opendataphilly.org/datasets/city-council-districts/"


def _document(links=(), **extra):
    raw = {"type": "Collection", "id": "points", "title": "Points", "links": list(links)}
    raw.update(extra)
    return read_document(raw, f"{BASE}/collection.json")


def _keys(table):
    return [cell.split("</td>")[0].rsplit(">", 1)[-1] for cell in table.split("<tr>")[1:]]


def test_license_cell():
    assert license_cell(_document()) is None
    assert license_cell(_document(license="CC-BY-4.0")) == "CC-BY-4.0"
    # One link carries the license name.
    one = _document([{"rel": "license", "href": PORTAL}], license="CC-BY-4.0")
    assert license_cell(one) == f'<a href="{PORTAL}">CC-BY-4.0</a>'
    unnamed = _document([{"rel": "license", "href": PORTAL, "title": "Open data terms"}])
    assert license_cell(unnamed) == f'<a href="{PORTAL}">Open data terms</a>'
    # Many links follow the name, and a repeated link shows once.
    links = [
        {"rel": "license", "href": PORTAL, "title": "License"},
        {"rel": "license", "href": TERMS, "title": "License"},
        {"rel": "license", "href": PORTAL, "title": "License"},
    ]
    many = license_cell(_document(links, license="other"))
    assert many.startswith("Custom terms: ")
    assert many.count(PORTAL) == 1 and many.count(escape(TERMS)) == 1
    assert ">opendataphilly.org<" in many and ">metadata.phila.gov<" in many
    assert license_cell(_document(links)).startswith("<a ")


def test_facts_are_one_row_each_in_order():
    links = [
        {"rel": "license", "href": PORTAL, "title": "License"},
        {"rel": "describedby", "href": "./README.md", "title": "Human-readable documentation"},
        {"rel": "via", "href": "https://opendataphilly.org/datasets/land-use/"},
    ]
    browser = "https://browser.test/#/external/points"
    table = facts_html(_document(links, license="other"), "Philadelphia", 2048, "#777", browser)
    assert _keys(table) == ["Publisher", "License", "Size", "Documentation", "Source", "More"]
    assert '<td style="color: #777; padding-right: 10px;">Publisher</td>' in table
    assert "<td>2.0 KB</td>" in table
    assert ">Human-readable documentation<" in table
    assert 'opendataphilly.org/datasets/land-use/">opendataphilly.org<' in table
    assert f'<a href="{browser}">{BROWSER_LINK}</a>' in table
    assert table.startswith("<table>") and table.endswith("</table>")


def test_facts_leave_out_what_is_not_known():
    assert facts_html(_document(), None, None, "#777") == "<table></table>"
    assert facts_html(_document(), "", 0, "#777") == "<table></table>"
    escaped = facts_html(_document(), "<b>Us</b>", None, "#777")
    assert "&lt;b&gt;Us&lt;/b&gt;" in escaped
