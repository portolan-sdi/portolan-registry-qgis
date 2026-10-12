"""The window shows each message through the translation, never as raw English.

scripts/i18n.py finds a string literal that skips _(). These tests find what a
static check cannot see: a message the code marks with N_() but shows without
_(), and a message translated at import, before the plugin sets the language.
A pseudo-translation wraps every message in brackets, so English text from the
catalog that shows without brackets skipped the translation.
"""

from __future__ import annotations

import gettext
import re

import pytest
from babel.messages.pofile import read_po
from qgis.core import QgsApplication, QgsProject, QgsSettings
from qgis.PyQt.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QTreeWidget,
    QWidget,
)

import portolan_registry_qgis
from portolan_registry_qgis.core import i18n
from portolan_registry_qgis.qgis_io.tileserver import TileServer

from .test_gui import FakeIface, open_collection, open_window

OPEN, CLOSE = "⟦", "⟧"
_FIELD = re.compile(r"\{[^{}]*\}")
_BRACKETED = re.compile(f"{OPEN}.*?{CLOSE}", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


class Pseudo(gettext.NullTranslations):
    """Wrap every message in brackets."""

    def gettext(self, message):
        return f"{OPEN}{message}{CLOSE}"

    def ngettext(self, msgid1, msgid2, n):
        return f"{OPEN}{msgid1 if n == 1 else msgid2}{CLOSE}"


def english_phrases() -> set[str]:
    """Return each run of two or more words in the English messages."""
    with i18n.LOCALE_DIR.joinpath("messages.pot").open("rb") as handle:
        template = read_po(handle)
    phrases = set()
    for message in template:
        ids = message.id if isinstance(message.id, tuple) else (message.id,)
        for text in ids:
            for piece in _FIELD.split(text):
                piece = piece.strip(" .,:;…")
                if len(piece.split()) >= 2:
                    phrases.add(piece)
    return phrases


def texts(root: QWidget) -> list[str]:
    """Return every text the window shows or offers as a tooltip."""
    found = [root.windowTitle()]
    for widget in [root, *root.findChildren(QWidget)]:
        found.append(widget.toolTip())
        if isinstance(widget, (QLabel, QAbstractButton)):
            found.append(widget.text())
        if isinstance(widget, QLineEdit):
            found.append(widget.placeholderText())
        if isinstance(widget, QGroupBox):
            found.append(widget.title())
        if isinstance(widget, QComboBox):
            found.extend(widget.itemText(i) for i in range(widget.count()))
        if isinstance(widget, QListWidget):
            for i in range(widget.count()):
                found.extend((widget.item(i).text(), widget.item(i).toolTip()))
        if isinstance(widget, QTreeWidget):
            header = widget.headerItem()
            found.extend(header.text(c) for c in range(header.columnCount()))
            for item in _items(widget):
                for column in range(widget.columnCount()):
                    found.extend((item.text(column), item.toolTip(column)))
    return [text for text in found if text]


def _items(tree: QTreeWidget):
    pending = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while pending:
        item = pending.pop()
        yield item
        pending.extend(item.child(i) for i in range(item.childCount()))


def untranslated(root: QWidget, phrases: set[str]) -> list[str]:
    """Return each shown text that holds English from the catalog outside brackets."""
    raw = []
    for text in texts(root):
        rest = _BRACKETED.sub("", _TAG.sub(" ", text))
        raw.extend(f"{phrase!r} in {text!r}" for phrase in phrases if phrase in rest)
    return raw


@pytest.fixture
def iface():
    fake = FakeIface()
    yield fake
    QgsProject.instance().clear()
    for widget in (fake.canvas, fake.bar, fake.window):
        widget.deleteLater()
    QgsApplication.processEvents()


@pytest.fixture
def server():
    tiles = TileServer()
    yield tiles
    tiles.stop()


@pytest.fixture
def pseudo():
    i18n.use(Pseudo(), "en")
    yield
    i18n.set_language(None)


@pytest.mark.usefixtures("pseudo")
def test_every_message_in_the_window_goes_through_the_translation(iface, server, catalog):
    phrases = english_phrases()
    window = open_window(iface, server, catalog)
    assert untranslated(window, phrases) == []
    open_collection(window)
    assert untranslated(window, phrases) == []
    window.disconnect_canvas()


@pytest.mark.usefixtures("pseudo")
def test_the_plugin_action_goes_through_the_translation(iface):
    plugin = portolan_registry_qgis.classFactory(iface)
    i18n.use(Pseudo(), "en")
    plugin.initGui()
    try:
        assert plugin.action.toolTip().startswith(OPEN)
    finally:
        plugin.unload()


@pytest.mark.parametrize("code", i18n.LANGUAGES)
def test_the_plugin_follows_the_qgis_language(code, iface):
    settings = QgsSettings()
    saved = settings.value("locale/userLocale", "")
    settings.setValue("locale/overrideFlag", True)
    settings.setValue("locale/userLocale", f"{code}_XX")
    try:
        portolan_registry_qgis.classFactory(iface)
        assert i18n.language() == code
        assert i18n._("Browse the Portolan registry") != "Browse the Portolan registry"
    finally:
        settings.setValue("locale/userLocale", saved)
        settings.remove("locale/overrideFlag")
        i18n.set_language(None)
