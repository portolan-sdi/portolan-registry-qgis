#!/usr/bin/env python3
"""Keep the Spanish and French translations level with the English source.

English is the source language. Its text is the key of every translation, so a
change to an English string leaves the old translation unmatched. This script
finds that gap and fails until a translation fills it.

Commands:

    update      Extract the English messages into messages.pot, merge them
                into each language's messages.po, compile messages.mo, and
                write the translated lines of metadata.txt.
    check       Fail when any translation lags the English. Pre-commit and CI
                run this.
    build-docs  Build the English documentation site into site/ and each
                translation into site/<code>/.

The English messages come from three sources:

- The plugin's Python code: the arguments of _(), N_(), ngettext(), and
  count() from portolan_registry_qgis.core.i18n.
- The description and about lines of metadata.txt, which the QGIS plugin
  manager shows. QGIS reads description[es] and about[es] for Spanish.
- The site_description and navigation titles of mkdocs.yml.

Each English page in docs/ has a translation at docs-i18n/<code>/<same path>.
Its front matter records the SHA-256 of the English page it translates. A
change to the English page changes the hash, and the check fails until the
translation is updated and records the new hash.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import io
import re
import shutil
import string
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from portolan_registry_qgis.core.i18n import DOMAIN, LANGUAGES  # noqa: E402

PACKAGE = ROOT / "portolan_registry_qgis"
LOCALE = PACKAGE / "locale"
POT = LOCALE / f"{DOMAIN}.pot"
METADATA = PACKAGE / "metadata.txt"
MKDOCS = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs"
DOCS_I18N = ROOT / "docs-i18n"
BUILD = ROOT / "build" / "i18n"
SITE = ROOT / "site"

PROJECT = "portolan-registry-qgis"
# A fixed date keeps messages.pot the same from run to run, so the check can
# compare it byte for byte.
STAMP = datetime.datetime(2026, 10, 12, tzinfo=datetime.timezone.utc)
METADATA_CONTEXT = "metadata.txt"
METADATA_KEYS = ("description", "about")
DOCS_CONTEXT = "docs"
# The plural helper in core/i18n.py, next to the gettext names Babel knows.
KEYWORDS = {"_": None, "N_": None, "ngettext": (1, 2), "count": (1, 2)}
TRANSLATORS = ("Translators:",)

# ---------- extraction ----------


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _metadata_lines() -> list[str]:
    return METADATA.read_text(encoding="utf-8").splitlines()


def _metadata_value(key: str) -> str:
    prefix = f"{key}="
    for line in _metadata_lines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    raise SystemExit(f"metadata.txt has no {key}= line")


def _mkdocs() -> dict[str, Any]:
    import yaml

    loaded = yaml.safe_load(MKDOCS.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SystemExit("mkdocs.yml is not a mapping")
    return loaded


def _nav_titles(nav: list[Any]) -> Iterator[str]:
    for entry in nav:
        if isinstance(entry, dict):
            for title, target in entry.items():
                yield str(title)
                if isinstance(target, list):
                    yield from _nav_titles(target)


def template() -> Any:
    """Return the catalog of every English message."""
    from babel.messages.catalog import Catalog
    from babel.messages.extract import extract_from_dir

    catalog = Catalog(
        project=PROJECT,
        domain=DOMAIN,
        creation_date=STAMP,
        revision_date=STAMP,
        fuzzy=False,
        charset="utf-8",
    )
    found = extract_from_dir(
        PACKAGE,
        method_map=[("**.py", "python")],
        keywords=KEYWORDS,
        comment_tags=TRANSLATORS,
        strip_comment_tags=True,
    )
    for filename, lineno, message, comments, context in found:
        path = _relative(PACKAGE / filename)
        catalog.add(message, locations=[(path, lineno)], auto_comments=comments, context=context)
    for key in METADATA_KEYS:
        catalog.add(
            _metadata_value(key),
            locations=[(_relative(METADATA), 0)],
            auto_comments=[f"The {key} line of the QGIS plugin manager."],
            context=METADATA_CONTEXT,
        )
    config = _mkdocs()
    docs_strings = [str(config["site_description"]), *_nav_titles(config.get("nav", []))]
    for text in docs_strings:
        catalog.add(text, locations=[(_relative(MKDOCS), 0)], context=DOCS_CONTEXT)
    return catalog


def _po_bytes(catalog: Any) -> bytes:
    from babel.messages.pofile import write_po

    buffer = io.BytesIO()
    write_po(
        buffer,
        catalog,
        width=79,
        sort_output=True,
        ignore_obsolete=True,
        # Babel writes the old English as #| lines but reads them back as
        # comments, so the file would never compare equal after a merge.
        include_previous=False,
        include_lineno=False,
    )
    # Babel ends the file with a blank line. end-of-file-fixer keeps one newline.
    return buffer.getvalue().rstrip(b"\n") + b"\n"


def _mo_bytes(catalog: Any) -> bytes:
    from babel.messages.mofile import write_mo

    buffer = io.BytesIO()
    write_mo(buffer, catalog)
    return buffer.getvalue()


def _po_path(code: str) -> Path:
    return LOCALE / code / "LC_MESSAGES" / f"{DOMAIN}.po"


def merged(code: str, source: Any) -> Any:
    """Return the language's catalog, merged with the current English messages."""
    from babel.messages.catalog import Catalog
    from babel.messages.pofile import read_po

    path = _po_path(code)
    if path.exists():
        with path.open("rb") as handle:
            catalog = read_po(handle, locale=code, domain=DOMAIN)
    else:
        catalog = Catalog(
            locale=code,
            domain=DOMAIN,
            project=PROJECT,
            creation_date=STAMP,
            revision_date=STAMP,
            fuzzy=False,
            charset="utf-8",
        )
    catalog.update(source, update_creation_date=False)
    catalog.obsolete.clear()
    return catalog


# ---------- metadata.txt ----------


def _translated(catalog: Any, text: str, context: str) -> str:
    message = catalog.get(text, context=context)
    if message is None or message.fuzzy or not message.string:
        return ""
    return str(message.string)


def metadata_text(catalogs: dict[str, Any]) -> str:
    """Return metadata.txt with a translated description and about per language."""
    keep = [
        line
        for line in _metadata_lines()
        if not re.match(rf"^({'|'.join(METADATA_KEYS)})\[[a-z_]+\]=", line)
    ]
    out: list[str] = []
    for line in keep:
        out.append(line)
        for key in METADATA_KEYS:
            if not line.startswith(f"{key}="):
                continue
            english = line[len(key) + 1 :]
            for code in LANGUAGES:
                text = _translated(catalogs[code], english, METADATA_CONTEXT)
                if text:
                    out.append(f"{key}[{code}]={text}")
    return "\n".join(out) + "\n"


# ---------- update ----------


def update() -> None:
    """Write messages.pot, each messages.po and messages.mo, and metadata.txt."""
    source = template()
    POT.parent.mkdir(parents=True, exist_ok=True)
    POT.write_bytes(_po_bytes(source))
    catalogs = {}
    for code in LANGUAGES:
        catalog = merged(code, source)
        catalogs[code] = catalog
        path = _po_path(code)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_po_bytes(catalog))
        path.with_suffix(".mo").write_bytes(_mo_bytes(catalog))
    METADATA.write_text(metadata_text(catalogs), encoding="utf-8")
    print(f"Wrote {_relative(POT)}, {len(LANGUAGES)} catalogs, and metadata.txt.")


# ---------- check ----------

_FIELD = string.Formatter()


def fields(text: str) -> set[str]:
    """Return the names of the str.format fields in ``text``."""
    try:
        return {name for _, name, _, _ in _FIELD.parse(text) if name is not None}
    except ValueError:
        return {"<unbalanced braces>"}


def _label(message: Any) -> str:
    ident = message.id[0] if isinstance(message.id, tuple) else message.id
    where = f" [{message.context}]" if message.context else ""
    return f"{ident!r}{where}"


def message_problems(code: str, catalog: Any) -> Iterator[str]:
    """Yield each message the language leaves untranslated or translates wrongly."""
    for message in catalog:
        if not message.id:
            continue
        label = _label(message)
        forms = message.string if isinstance(message.string, tuple) else (message.string,)
        if message.fuzzy:
            yield f"{code}: {label} is fuzzy. The English changed. Check the translation."
            continue
        if not all(forms):
            yield f"{code}: {label} has no translation."
            continue
        ids = message.id if isinstance(message.id, tuple) else (message.id,)
        expected = set().union(*(fields(text) for text in ids))
        for form in forms:
            if fields(form) != expected:
                yield (
                    f"{code}: {label} uses the fields {sorted(fields(form))}, "
                    f"but the English uses {sorted(expected)}."
                )
        if message.context == METADATA_CONTEXT and any("\n" in form for form in forms):
            yield f"{code}: {label} must stay on one line in metadata.txt."


def catalog_problems() -> Iterator[str]:
    """Yield each way the catalogs and metadata.txt lag the English source."""
    source = template()
    if not POT.exists() or POT.read_bytes() != _po_bytes(source):
        yield f"{_relative(POT)} does not match the English in the code. Run: {_UPDATE}"
    catalogs = {}
    for code in LANGUAGES:
        path = _po_path(code)
        catalog = merged(code, source)
        catalogs[code] = catalog
        if not path.exists() or path.read_bytes() != _po_bytes(catalog):
            yield f"{_relative(path)} is not merged with messages.pot. Run: {_UPDATE}"
        mo = path.with_suffix(".mo")
        if not mo.exists() or mo.read_bytes() != _mo_bytes(catalog):
            yield f"{_relative(mo)} is not compiled from messages.po. Run: {_UPDATE}"
        yield from message_problems(code, catalog)
    if METADATA.read_text(encoding="utf-8") != metadata_text(catalogs):
        yield f"{_relative(METADATA)} does not hold the current translations. Run: {_UPDATE}"
    present = sorted(p.name for p in LOCALE.iterdir() if p.is_dir()) if LOCALE.exists() else []
    if present != sorted(LANGUAGES):
        yield f"{_relative(LOCALE)} holds {present}, but core/i18n.py LANGUAGES is {sorted(LANGUAGES)}."


_UPDATE = "uv run python scripts/i18n.py update"

# ---------- documentation ----------

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_SOURCE = re.compile(r"^source_sha256: *([0-9a-f]{64}) *$", re.MULTILINE)
VALE_OFF = "<!-- vale off -->"


def sha256(path: Path) -> str:
    """Return the SHA-256 of a file, as hex."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def english_pages() -> list[Path]:
    """Return the Markdown pages of the English site, relative to docs/."""
    return sorted(p.relative_to(DOCS) for p in DOCS.rglob("*.md"))


def page_problems(code: str, page: Path) -> Iterator[str]:
    """Yield each way one translated page lags its English page."""
    english = DOCS / page
    translated = DOCS_I18N / code / page
    if not translated.exists():
        yield f"{_relative(translated)} is missing. Translate {_relative(english)}."
        return
    text = translated.read_text(encoding="utf-8")
    front = _FRONT.match(text)
    recorded = _SOURCE.search(front.group(1)) if front else None
    if recorded is None:
        yield f"{_relative(translated)} has no source_sha256 in its front matter."
    elif recorded.group(1) != sha256(english):
        yield (
            f"{_relative(translated)} translates an old version of {_relative(english)}. "
            f"Translate the change, then write source_sha256: {sha256(english)}"
        )
    body = text[front.end() :] if front else text
    if not body.lstrip().startswith(VALE_OFF):
        yield f"{_relative(translated)} must start with {VALE_OFF} after its front matter."


def docs_problems() -> Iterator[str]:
    """Yield each translated page that is missing, stale, or has no English page."""
    pages = english_pages()
    for code in LANGUAGES:
        for page in pages:
            yield from page_problems(code, page)
        root = DOCS_I18N / code
        extra = sorted(p.relative_to(root) for p in root.rglob("*")) if root.exists() else []
        for path in extra:
            if path.suffix == ".md" and path not in pages:
                yield f"{_relative(root / path)} has no English page in docs/. Remove it."
            elif path.suffix != ".md" and not (root / path).is_dir():
                yield f"{_relative(root / path)} is not a page. Keep assets in docs/."
    alternate = {entry.get("lang") for entry in _mkdocs().get("extra", {}).get("alternate", [])}
    if alternate != {"en", *LANGUAGES}:
        yield f"mkdocs.yml extra.alternate names {sorted(alternate)}, not en and {list(LANGUAGES)}."


# ---------- source lint ----------

# Text a person reads: two words, or one capitalized word such as "Download".
_PROSE = re.compile(r"[A-Za-z][a-z]+[ .,:;?!…]+[a-z]{2,}|^[A-Z][a-z]{2,}[.…]?$")
_TRANSLATE = {"_", "N_", "ngettext", "count"}
_IMPORT_TIME = {"_", "ngettext", "count"}
_SKIP = "# i18n: skip"


def _called(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _docstrings(tree: ast.Module) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            found.add(id(node.value))
    return found


def _exempt(tree: ast.Module) -> set[int]:
    """Return the ids of nodes whose strings a person never reads."""
    exempt = _docstrings(tree)
    for node in ast.walk(tree):
        inside: list[ast.AST] = []
        if _called(node) in _TRANSLATE:
            inside = list(node.args) if isinstance(node, ast.Call) else []
        elif isinstance(node, ast.Compare):
            inside = [node.left, *node.comparators]
        elif isinstance(node, ast.Subscript):
            inside = [node.slice]
        elif isinstance(node, ast.Dict):
            inside = [key for key in node.keys if key is not None]
        for part in inside:
            exempt.update(id(child) for child in ast.walk(part))
    return exempt


def _import_time_calls(tree: ast.Module) -> Iterator[ast.Call]:
    """Yield the translation calls that run when the module is imported."""

    def walk(nodes: list[ast.stmt]) -> Iterator[ast.Call]:
        for statement in nodes:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = [*statement.args.defaults, *statement.args.kw_defaults]
                for default in defaults:
                    if default is not None:
                        yield from _calls_in(default)
                continue
            if isinstance(statement, ast.ClassDef):
                yield from walk(statement.body)
                continue
            yield from _calls_in(statement)

    yield from walk(tree.body)


def _calls_in(node: ast.AST) -> Iterator[ast.Call]:
    """Yield the translation calls in ``node``, except in a lambda, which runs later."""
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, ast.Call) and _called(node) in _IMPORT_TIME:
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _calls_in(child)


def lint_source(path: Path, source: str) -> Iterator[str]:
    """Yield each string a person reads that skips translation, in one module."""
    tree = ast.parse(source)
    lines = source.splitlines()
    exempt = _exempt(tree)
    where = _relative(path) if path.is_absolute() else str(path)
    for call in _import_time_calls(tree):
        yield (
            f"{where}:{call.lineno}: {_called(call)}() runs at import, before the plugin "
            "sets the language. Mark the text with N_() and call _() where it is shown."
        )
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            kind = "an f-string"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text, kind = node.value, "a string"
        else:
            continue
        if not _PROSE.search(text):
            continue
        span = lines[node.lineno - 1 : (node.end_lineno or node.lineno)]
        if any(_SKIP in line for line in span):
            continue
        exempt.update(id(child) for child in ast.walk(node))
        yield (
            f"{where}:{node.lineno}: {kind} a person reads skips translation: "
            f"{text[:60]!r}. Wrap it in _(), or add '{_SKIP}' when no person reads it."
        )


def lint_problems() -> Iterator[str]:
    """Yield each untranslated string in the plugin package."""
    for path in sorted(PACKAGE.rglob("*.py")):
        yield from lint_source(path, path.read_text(encoding="utf-8"))


def check() -> int:
    """Print every translation problem and return the exit status."""
    problems = [*lint_problems(), *catalog_problems(), *docs_problems()]
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} translation problems. See docs/development.md, Translations.")
        return 1
    return 0


# ---------- documentation build ----------


def language_config(code: str, catalog: Any) -> dict[str, Any]:
    """Return mkdocs.yml as the site in language ``code`` needs it."""
    config = _mkdocs()

    def tr(text: str) -> str:
        return _translated(catalog, text, DOCS_CONTEXT) or text

    def nav(entries: list[Any]) -> list[Any]:
        out: list[Any] = []
        for entry in entries:
            if isinstance(entry, dict):
                out.append(
                    {
                        tr(str(title)): nav(target) if isinstance(target, list) else target
                        for title, target in entry.items()
                    }
                )
            else:
                out.append(entry)
        return out

    config["site_url"] = f"{str(config['site_url']).rstrip('/')}/{code}/"
    config["site_description"] = tr(str(config["site_description"]))
    config["theme"]["language"] = code
    config["edit_uri"] = f"edit/main/docs-i18n/{code}/"
    config["nav"] = nav(config.get("nav", []))
    config["docs_dir"] = f"build/i18n/{code}"
    config["site_dir"] = f"site/{code}"
    return config


def build_docs() -> None:
    """Build the English site into site/ and each translation into site/<code>/."""
    import yaml
    from babel.messages.pofile import read_po

    zensical = [sys.executable, "-m", "zensical", "build", "--strict", "--clean"]
    subprocess.run(zensical, cwd=ROOT, check=True)  # noqa: S603 - fixed arguments
    for code in LANGUAGES:
        with _po_path(code).open("rb") as handle:
            catalog = read_po(handle, locale=code, domain=DOMAIN)
        target = BUILD / code
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(DOCS, target, ignore=shutil.ignore_patterns("*.md"))
        shutil.copytree(DOCS_I18N / code, target, dirs_exist_ok=True)
        config = ROOT / f"mkdocs.{code}.yml"
        config.write_text(
            yaml.safe_dump(language_config(code, catalog), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        try:
            subprocess.run([*zensical, "-f", config.name], cwd=ROOT, check=True)  # noqa: S603
        finally:
            config.unlink()
        print(f"Built the {code} site into site/{code}/.")


def main() -> int:
    """Run one command."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("update", "check", "build-docs"))
    args = parser.parse_args()
    if args.command == "update":
        update()
        return 0
    if args.command == "build-docs":
        build_docs()
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(main())
