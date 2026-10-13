"""Write the facts of a document as the HTML table of the Details section.

Each fact is one row, and a row leaves out what is not known. The license
links make the License row, the publisher's documentation makes the
Documentation row, the ``via`` links make the Source row, and the browser
page makes the More row. A link that two STAC links repeat shows once.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from portolan_registry_qgis.core.format import human_size, license_name, link_label
from portolan_registry_qgis.core.i18n import N_, _

if TYPE_CHECKING:
    from portolan_registry_qgis.core.stac import Document

BROWSER_LINK = N_("All files and metadata in the Portolan browser")


def _anchor(href: str, text: str) -> str:
    return f'<a href="{html.escape(href)}">{html.escape(text)}</a>'


def _links(document: Document, rel: str) -> list[tuple[str, str | None]]:
    """Return the href and title of each ``rel`` link, once per href."""
    found: dict[str, str | None] = {}
    for link in document.links:
        if link.rel == rel and link.href not in found:
            found[link.href] = link.title
    return list(found.items())


def _links_cell(document: Document, rel: str) -> str | None:
    """Return the ``rel`` links as anchors that commas separate, or None."""
    links = _links(document, rel)
    return ", ".join(_anchor(href, link_label(href, title)) for href, title in links) or None


def license_cell(document: Document) -> str | None:
    """Return the License cell, or None when the document names no license.

    One license link carries the license name. Many links follow the name.
    """
    name = license_name(document.license)
    licenses = _links(document, "license")
    if len(licenses) == 1:
        return _anchor(licenses[0][0], name or link_label(*licenses[0]))
    if licenses:
        linked = ", ".join(_anchor(href, link_label(href, title)) for href, title in licenses)
        return f"{html.escape(name)}: {linked}" if name else linked
    return html.escape(name) if name else None


def facts_html(
    document: Document,
    publisher: str | None,
    size: int | None,
    label_color: str,
    browser: str | None = None,
) -> str:
    """Return the facts of ``document`` as one two-column table.

    ``label_color`` is a CSS color for the labels, such as "#7a7a72".
    ``browser`` is the document's page in the Portolan browser.
    """
    rows = (
        (_("Publisher"), html.escape(publisher) if publisher else None),
        (_("License"), license_cell(document)),
        (_("Size"), html.escape(human_size(size)) if size else None),
        (_("Documentation"), _links_cell(document, "describedby")),
        # Translators: the label of the row that links to where the data comes from.
        (_("Source"), _links_cell(document, "via")),
        # Translators: the label of the row that links to more about the dataset.
        (_("More"), _anchor(browser, _(BROWSER_LINK)) if browser else None),
    )
    cells = "".join(
        f'<tr><td style="color: {label_color}; padding-right: 10px;">{html.escape(key)}</td>'  # i18n: skip
        f"<td>{value}</td></tr>"
        for key, value in rows
        if value is not None
    )
    return f"<table>{cells}</table>"
