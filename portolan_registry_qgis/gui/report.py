"""Open the GitHub forms that report a dataset problem or a plugin bug."""

from __future__ import annotations

import datetime
import platform
from pathlib import Path

from osgeo import gdal
from qgis.core import Qgis
from qgis.PyQt.QtCore import QT_VERSION_STR

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.report import Versions, bug_url, problem_url, read_metadata
from portolan_registry_qgis.gui.widgets import open_url

METADATA = Path(__file__).parents[1] / "metadata.txt"  # i18n: skip


def bug_label() -> str:
    """Return the text of the action that opens the plugin problem form."""
    return _("Report a bug")


def versions() -> Versions:
    """Return the versions of the plugin, QGIS, Qt, Python, GDAL, DuckDB, and the OS."""
    plugin, _tracker = read_metadata(METADATA)
    return Versions(
        plugin=plugin,
        qgis=Qgis.version(),
        qt=QT_VERSION_STR,
        python=platform.python_version(),
        gdal=gdal.__version__,
        duckdb=parquet_query.duckdb_status()[1],
        os=platform.platform(),
    )


def open_bug_report() -> None:
    """Open the plugin problem form, with the versions filled in."""
    _version, tracker = read_metadata(METADATA)
    open_url(bug_url(tracker, versions()))


def open_problem_report(catalog_id: str, href: str, link: str | None) -> None:
    """Open the registry's dataset problem form for the document at ``href`` of ``catalog_id``."""
    open_url(
        problem_url(catalog_id, href, link, datetime.datetime.now().astimezone().date(), versions())
    )
