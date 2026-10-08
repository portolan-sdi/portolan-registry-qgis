"""Portolan Registry: browse, load, and download Portolan catalogs in QGIS."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parent
if (_PLUGIN_ROOT / "portolan").is_dir() and str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


def classFactory(iface: Any) -> Any:  # noqa: N802 - QGIS calls this name
    """Return the plugin instance. QGIS calls this when it loads the plugin."""
    from portolan_registry_qgis.plugin import PortolanRegistryPlugin

    return PortolanRegistryPlugin(iface)
