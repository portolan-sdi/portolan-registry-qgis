"""Write to the QGIS message log under the plugin's tag."""

from __future__ import annotations

from qgis.core import Qgis, QgsMessageLog

# The tab in the QGIS message log, and the title of the plugin's messages.
LOG_TAG = "Portolan Registry"


def log(message: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Warning) -> None:
    """Write ``message`` to the plugin's tab of the QGIS message log."""
    QgsMessageLog.logMessage(message, LOG_TAG, level)
