from __future__ import annotations

import gettext

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo

from portolan_registry_qgis.core import i18n
from portolan_registry_qgis.core.format import catalog_summary, human_size, license_name


@pytest.fixture(autouse=True)
def english():
    i18n.set_language(None)
    yield
    i18n.set_language(None)


def _compile(directory, code, messages):
    catalog = Catalog(locale=code, domain=i18n.DOMAIN)
    for source, translation in messages.items():
        catalog.add(source, translation)
    folder = directory / code / "LC_MESSAGES"
    folder.mkdir(parents=True)
    with (folder / f"{i18n.DOMAIN}.mo").open("wb") as handle:
        write_mo(handle, catalog)


@pytest.mark.parametrize(
    ("locale", "code"),
    [
        ("es_AR", "es"),
        ("es", "es"),
        ("fr-CA", "fr"),
        ("FR_fr", "fr"),
        ("de", "en"),
        ("en_US", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
def test_language_code(locale, code):
    assert i18n.language_code(locale) == code


def test_set_language_english_uses_the_source_text():
    assert i18n.set_language("en_GB") == "en"
    assert i18n.language() == "en"
    assert type(i18n._State.translation) is gettext.NullTranslations
    assert i18n._("Vector tiles") == "Vector tiles"


def test_set_language_without_a_catalog_falls_back_to_english(tmp_path):
    assert i18n.set_language("es_ES", directory=tmp_path) == "es"
    assert i18n._("Vector tiles") == "Vector tiles"
    assert i18n.count("{n} file", "{n} files", 2) == "2 files"


@pytest.mark.parametrize(
    ("code", "whole", "fraction"),
    [
        ("en", "1,234,567", "1,234.5"),
        ("es", "1.234.567", "1.234,5"),
        # CLDR groups French digits with a narrow no-break space.
        ("fr", "1\u202f234\u202f567", "1\u202f234,5"),
    ],
)
def test_numbers_follow_the_language(code, whole, fraction):
    i18n.use(gettext.NullTranslations(), code)
    assert i18n.number(1_234_567) == whole
    assert i18n.decimal(1234.5) == fraction


def test_decimal_digits():
    assert i18n.decimal(1.25, 2) == "1.25"
    assert i18n.decimal(999.0, 0) == "999"


def test_unknown_number_language_uses_english():
    i18n.use(gettext.NullTranslations(), "de")
    assert i18n.language() == "en"
    assert i18n.number(1200) == "1,200"


def test_count():
    assert i18n.count("{n} file", "{n} files", 1) == "1 file"
    assert i18n.count("{n} file", "{n} files", 0) == "0 files"
    assert i18n.count("{n} file", "{n} files", 1200) == "1,200 files"


def test_n_returns_the_message_unchanged():
    message = "Custom terms"
    assert i18n.N_(message) is message


def test_compiled_spanish_catalog(tmp_path):
    _compile(
        tmp_path,
        "es",
        {
            "Vector tiles": "Teselas vectoriales",
            "Custom terms": "Condiciones propias",
            ("{n} collection", "{n} collections"): ("{n} colección", "{n} colecciones"),
            "{first} and {second}": "{first} y {second}",
        },
    )
    assert i18n.set_language("es_ES", directory=tmp_path) == "es"
    assert i18n._("Vector tiles") == "Teselas vectoriales"
    assert i18n._("Not in the catalog") == "Not in the catalog"
    assert license_name("other") == "Condiciones propias"
    # The catalog leaves "GB" as it is, and the number takes a Spanish comma.
    assert human_size(1_400_000_000) == "1,4 GB"
    assert i18n.count("{n} collection", "{n} collections", 1) == "1 colección"
    assert i18n.count("{n} collection", "{n} collections", 1200) == "1.200 colecciones"
    assert (
        catalog_summary(1200, None, ("CC0-1.0", "ODbL-1.0"))
        == "1.200 colecciones, CC0-1.0 y ODbL-1.0"
    )


def test_compiled_french_catalog_translates_byte_units(tmp_path):
    _compile(tmp_path, "fr", {"B": "o", "GB": "Go"})
    assert i18n.set_language("fr", directory=tmp_path) == "fr"
    assert human_size(1_400_000_000) == "1,4 Go"
    assert human_size(999) == "999 o"


def test_english_is_restored_after_a_translated_test():
    assert i18n.language() == "en"
    assert human_size(1_400_000_000) == "1.4 GB"
