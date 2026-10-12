from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from babel.messages.catalog import Catalog

from scripts import i18n


def lint(source: str) -> list[str]:
    return list(i18n.lint_source(Path("module.py"), source))


def test_lint_flags_a_label_that_skips_translation():
    problems = lint('button.setText("Zoom to")\n')
    assert len(problems) == 1
    assert "module.py:1" in problems[0]
    assert "'Zoom to'" in problems[0]


def test_lint_flags_a_single_capitalized_word():
    assert lint('button.setText("Download")\n')


def test_lint_flags_an_f_string():
    problems = lint('status.setText(f"Could not open {url}")\n')
    assert len(problems) == 1
    assert "an f-string" in problems[0]


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    button.setText(_("Zoom to"))\n',
        'def f():\n    row.setText(count("{n} file", "{n} files", n))\n',
        'TIP = N_("Fast. Draws with the style.")\n',
        'def f():\n    """Return the thing a person reads."""\n',
        'if kind == "Point layer":\n    pass\n',
        'value = data["display name"]\n',
        'labels = {"Custom terms": 1}\n',
        'KEY = "PortolanRegistry/registry_url"\n',
        'sql = "SELECT count(*) FROM read_parquet(?)"\n',
        'tag = "Portolan Registry"\n',
        'note = "Internal words here"  # i18n: skip\n',
    ],
)
def test_lint_accepts(source):
    assert lint(source) == []


def test_lint_flags_a_translation_at_import():
    problems = lint('TIP = _("Fast. Draws with the style.")\n')
    assert len(problems) == 1
    assert "runs at import" in problems[0]


def test_lint_flags_a_translation_in_a_class_body_and_a_default():
    source = (
        'class Window:\n    TITLE = _("Portolan registry window")\n'
        'def show(text=_("Show more")):\n    pass\n'
    )
    assert sum("runs at import" in p for p in lint(source)) == 2


def test_lint_accepts_a_translation_inside_a_function_or_lambda():
    source = 'def show():\n    return _("Show more")\nlabel = lambda: _("Show more")\n'
    assert lint(source) == []


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Could not open {url}: {error}", {"url", "error"}),
        ("{n} files", {"n"}),
        ("No fields", set()),
        ("{{literal}}", set()),
        ("Broken {", {"<unbalanced braces>"}),
    ],
)
def test_fields(text, names):
    assert i18n.fields(text) == names


def catalog(*messages: tuple[object, object, dict]) -> Catalog:
    result = Catalog(locale="es")
    for ident, string, options in messages:
        result.add(ident, string, **options)
    return result


def test_message_problems_accepts_a_complete_translation():
    entries = catalog(
        ("Could not open {url}", "No se pudo abrir {url}", {}),
        (("{n} file", "{n} files"), ("{n} archivo", "{n} archivos"), {}),
    )
    assert list(i18n.message_problems("es", entries)) == []


def test_message_problems_reports_a_missing_translation():
    problems = list(i18n.message_problems("es", catalog(("Zoom to", "", {}))))
    assert problems == ["es: 'Zoom to' has no translation."]


def test_message_problems_reports_a_missing_plural_form():
    entries = catalog((("{n} file", "{n} files"), ("{n} archivo", ""), {}))
    assert list(i18n.message_problems("es", entries)) == ["es: '{n} file' has no translation."]


def test_message_problems_reports_a_fuzzy_translation():
    entries = catalog(("Zoom to it", "Acercar", {"flags": ["fuzzy"]}))
    problems = list(i18n.message_problems("es", entries))
    assert len(problems) == 1
    assert "fuzzy" in problems[0]


def test_message_problems_reports_a_changed_field():
    entries = catalog(("Could not open {url}", "No se pudo abrir {enlace}", {}))
    problems = list(i18n.message_problems("es", entries))
    assert len(problems) == 1
    assert "['enlace']" in problems[0]
    assert "['url']" in problems[0]


def test_message_problems_keeps_metadata_on_one_line():
    entries = catalog(("About", "Uno\nDos", {"context": i18n.METADATA_CONTEXT}))
    assert any("one line" in p for p in i18n.message_problems("es", entries))


@pytest.fixture
def docs(tmp_path, monkeypatch):
    english = tmp_path / "docs"
    english.mkdir()
    (english / "index.md").write_text("# Home\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "ROOT", tmp_path)
    monkeypatch.setattr(i18n, "DOCS", english)
    monkeypatch.setattr(i18n, "DOCS_I18N", tmp_path / "docs-i18n")
    return tmp_path


def translate(root: Path, text: str, digest: str | None = None) -> Path:
    page = root / "docs-i18n" / "es" / "index.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    english = (root / "docs" / "index.md").read_bytes()
    digest = digest or hashlib.sha256(english).hexdigest()
    page.write_text(f"---\nsource_sha256: {digest}\n---\n\n{text}", encoding="utf-8")
    return page


def test_page_problems_accepts_a_current_translation(docs):
    translate(docs, f"{i18n.VALE_OFF}\n\n# Inicio\n")
    assert list(i18n.page_problems("es", Path("index.md"))) == []


def test_page_problems_reports_a_missing_page(docs):
    problems = list(i18n.page_problems("es", Path("index.md")))
    assert problems == ["docs-i18n/es/index.md is missing. Translate docs/index.md."]


def test_page_problems_reports_a_stale_page(docs):
    translate(docs, f"{i18n.VALE_OFF}\n\n# Inicio\n")
    (docs / "docs" / "index.md").write_text("# Home page\n", encoding="utf-8")
    problems = list(i18n.page_problems("es", Path("index.md")))
    assert len(problems) == 1
    assert "translates an old version of docs/index.md" in problems[0]
    assert hashlib.sha256(b"# Home page\n").hexdigest() in problems[0]


def test_page_problems_reports_a_page_vale_checks(docs):
    translate(docs, "# Inicio\n")
    problems = list(i18n.page_problems("es", Path("index.md")))
    assert problems == [
        f"docs-i18n/es/index.md must start with {i18n.VALE_OFF} after its front matter."
    ]


def test_page_problems_reports_a_page_with_no_hash(docs):
    page = docs / "docs-i18n" / "es" / "index.md"
    page.parent.mkdir(parents=True)
    page.write_text(f"{i18n.VALE_OFF}\n# Inicio\n", encoding="utf-8")
    problems = list(i18n.page_problems("es", Path("index.md")))
    assert problems == ["docs-i18n/es/index.md has no source_sha256 in its front matter."]


def test_language_config_translates_the_navigation():
    entries = Catalog(locale="es")
    entries.add("Install", "Instalación", context=i18n.DOCS_CONTEXT)
    config = i18n.language_config("es", entries)
    assert config["site_url"] == "https://portolan-sdi.github.io/portolan-registry-qgis/es/"
    assert config["theme"]["language"] == "es"
    assert {"Instalación": "install.md"} in config["nav"]
    assert config["docs_dir"] == "build/i18n/es"
    assert config["site_dir"] == "site/es"


def test_the_repository_translations_are_complete():
    """The same gate as the pre-commit hook, so the test matrix enforces it too."""
    problems = [*i18n.lint_problems(), *i18n.catalog_problems(), *i18n.docs_problems()]
    assert problems == []
