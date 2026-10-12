"""Portolan Registry: browse, load, and download Portolan catalogs in QGIS."""

from __future__ import annotations

from typing import Any


def classFactory(iface: Any) -> Any:  # noqa: N802 - QGIS calls this name
    """Return the plugin instance. QGIS calls this when it loads the plugin."""
    from qgis.core import QgsApplication

    from portolan_registry_qgis.core.i18n import set_language

    # Set the language before the plugin modules load. Their constants hold
    # English in N_(), and each widget translates its text when it is built,
    # so the language must be known first.
    set_language(QgsApplication.locale())
    from portolan_registry_qgis.plugin import PortolanRegistryPlugin

    return PortolanRegistryPlugin(iface)
