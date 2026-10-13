"""Write to the QGIS message log and the message bar under the plugin's tag."""

from __future__ import annotations

from typing import Any

from qgis.core import Qgis, QgsMessageLog

# The tab in the QGIS message log, and the title of the plugin's messages.
LOG_TAG = "Portolan Registry"


def log(message: str, level: Qgis.MessageLevel = Qgis.MessageLevel.Warning) -> None:
    """Write ``message`` to the plugin's tab of the QGIS message log."""
    QgsMessageLog.logMessage(message, LOG_TAG, level)


class Messages:
    """Show the plugin's messages in the QGIS message bar."""

    def __init__(self, iface: Any):
        self._iface = iface

    def info(self, text: str) -> None:
        """Show ``text`` for 5 seconds."""
        self._iface.messageBar().pushMessage(LOG_TAG, text, Qgis.MessageLevel.Info, 5)

    def warn(self, text: str) -> None:
        """Log ``text`` as a warning, and show it for 10 seconds."""
        log(text, Qgis.MessageLevel.Warning)
        self._iface.messageBar().pushMessage(LOG_TAG, text, Qgis.MessageLevel.Warning, 10)
