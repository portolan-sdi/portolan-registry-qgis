"""Read STAC documents in the background, and keep each one that was read.

The tree, the details panel, and the item rows read the same documents. The
cache reads each href once. A caller that asks while a read runs waits for
that read, and a later caller gets the kept document at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.stac import Document, read_document
from portolan_registry_qgis.qgis_io.network import fetch_json, run_task

if TYPE_CHECKING:
    from collections.abc import Callable

    Opened = Callable[[object, BaseException | None], None]


class DocumentCache:
    """Read each STAC document once, and share the result with every caller."""

    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}
        self._reading: dict[str, list[Opened]] = {}

    def open(self, href: str, then: Opened, *, quiet: bool = False) -> None:
        """Call ``then`` with the document at ``href``, or with None and the error.

        A kept document calls ``then`` at once. Otherwise ``then`` runs on the
        main thread when the read ends. ``quiet`` keeps the read out of the
        QGIS task manager.
        """
        cached = self._documents.get(href)
        if cached is not None:
            then(cached, None)
            return
        waiting = self._reading.setdefault(href, [])
        waiting.append(then)
        if len(waiting) > 1:
            return

        def read(_task: object) -> Document:
            return read_document(fetch_json(href), href)

        def done(result: object, error: BaseException | None) -> None:
            if isinstance(result, Document):
                self._documents[href] = result
            for callback in self._reading.pop(href, []):
                callback(result, error)

        # Translators: a task in the QGIS task manager that reads the document at {url}.
        run_task(_("Read {url}").format(url=href), read, done, hidden=quiet)
