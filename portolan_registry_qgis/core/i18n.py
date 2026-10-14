"""Translate the text the plugin shows, and write numbers the way the language does.

The English text of a message is its key. Call ``_`` when the text shows,
never at import. Mark module constants with ``N_``.
"""

from __future__ import annotations

import gettext
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent.parent / "locale"
DOMAIN = "messages"
SOURCE_LANGUAGE = "en"
# The translated languages. scripts/i18n.py requires a complete catalog, a
# metadata.txt entry, and a docs site for each one.
LANGUAGES = ("es", "fr")

# The separators the Unicode CLDR gives each language, as Qt's QLocale uses them.
# French groups digits with a narrow no-break space (U+202F).
_GROUP = {"en": ",", "es": ".", "fr": "\u202f"}
_DECIMAL = {"en": ".", "es": ",", "fr": ","}


class State:
    """The language and the catalog that the plugin shows."""

    language = SOURCE_LANGUAGE
    translation: gettext.NullTranslations = gettext.NullTranslations()


def language_code(locale: str | None) -> str:
    """Return the language the plugin shows for a locale such as "es_AR".

    A locale with no catalog gets English.
    """
    if not locale:
        return SOURCE_LANGUAGE
    code = locale.replace("-", "_").split("_")[0].lower()
    return code if code in LANGUAGES else SOURCE_LANGUAGE


def set_language(locale: str | None, directory: Path = LOCALE_DIR) -> str:
    """Show text in the language of ``locale`` from now on, and return its code.

    Args:
        locale: A locale name, such as QGIS's "fr" or "es_AR".
        directory: The folder that holds the compiled catalogs.
    """
    code = language_code(locale)
    if code == SOURCE_LANGUAGE:
        use(gettext.NullTranslations(), code)
    else:
        use(gettext.translation(DOMAIN, directory, languages=[code], fallback=True), code)
    return code


def use(translation: gettext.NullTranslations, code: str) -> None:
    """Show text through ``translation``, with the numbers of language ``code``."""
    State.translation = translation
    State.language = code if code in _GROUP else SOURCE_LANGUAGE


def language() -> str:
    """Return the code of the language the plugin shows, such as "en"."""
    return State.language


def _(message: str) -> str:
    """Return ``message`` in the current language."""
    return State.translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Return the form of a message that fits ``n``, in the current language."""
    return State.translation.ngettext(singular, plural, n)


def N_(message: str) -> str:  # noqa: N802 - the gettext name for a deferred mark
    """Mark ``message`` for translation and return it unchanged.

    A constant uses this, and the code that shows it calls :func:`_`.
    """
    return message


def count(singular: str, plural: str, n: int) -> str:
    """Return a message about ``n`` things, with ``{n}`` written as a number.

    For example, ``count("{n} file", "{n} files", 1200)`` is "1,200 files".
    """
    return ngettext(singular, plural, n).format(n=number(n))


def number(value: int) -> str:
    """Return ``value`` with the digit groups of the current language."""
    return f"{value:,}".replace(",", _GROUP[State.language])


def decimal(value: float, digits: int = 1) -> str:
    """Return ``value`` to ``digits`` places, with the separators of the current language."""
    text = f"{value:,.{digits}f}"
    return "".join(
        _GROUP[State.language] if c == "," else _DECIMAL[State.language] if c == "." else c
        for c in text
    )
