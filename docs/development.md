# Development

The plugin has two test suites.

- `tests/core` covers the STAC, registry, PMTiles, GeoParquet, and download logic in `portolan_registry_qgis/core`. That package imports neither QGIS nor GDAL, so the suite runs in the uv environment.
- `tests/in_qgis` needs PyQGIS. It builds a small catalog with GDAL and DuckDB and serves it on loopback. The tests then exercise the network code, the layers, the downloads, and the window.

```bash
uv sync
uv run pytest
```

For the QGIS suite, make a virtual environment on the Python that has PyQGIS:

```bash
uv venv --python /usr/bin/python3 --system-site-packages .venv-qgis
VIRTUAL_ENV=.venv-qgis uv pip install pytest pytest-timeout duckdb babel
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python -m pytest tests/in_qgis
```

Add `-m network` to run the live checks against the registry and two published catalogs.

CI runs `tests/in_qgis` in the official QGIS 3.34, 3.44, and 4.2 Docker images. QGIS 3.34 is the oldest version the plugin supports, and 4.2 covers Qt6.

To time PMTiles rendering against a remote archive, run the benchmark with the same Python. The `--baseline` flag restores the 0.1.1 tile transport for comparison.

```bash
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python \
  scripts/bench_tiles.py
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python \
  scripts/bench_tiles.py --baseline
```

## Translations

The plugin and this site are in English, Spanish, and French. English is the source language, and a change to English text needs the Spanish and French translations in the same pull request. Pre-commit and CI run `scripts/i18n.py check`, which fails until both languages have caught up.

The plugin translates its text with gettext. Wrap each string a person reads in `_()` from `portolan_registry_qgis.core.i18n`, and use `count()` for text with a number:

```python
from portolan_registry_qgis.core.i18n import _, count

label.setText(_("Zoom to"))
status.setText(_("Could not open {url}").format(url=url))
row.setText(count("{n} file", "{n} files", total))
```

Because the plugin chooses the language after Python imports its modules, a module constant holds its English in `N_()`, and the code that shows it calls `_()`. Placeholders take names, which lets a translation reorder them. A comment that starts with `# Translators:` on the line above a call reaches the translator.

After you change English text, update the catalogs, then translate:

```bash
uv run python scripts/i18n.py update
uv run python scripts/i18n.py check
```

`update` extracts the English into `portolan_registry_qgis/locale/messages.pot` and merges it into each language's `messages.po`. A new string arrives with no translation. A changed string arrives marked `#, fuzzy` with its old translation. Write the translation, remove the `fuzzy` flag, then run `update` again. That run compiles `messages.mo` and writes the `description[es]` and `about[es]` lines of `metadata.txt`, which the QGIS plugin manager shows.

Each page in `docs/` has a translation at `docs-i18n/<lang>/` with the same path. Its front matter records the SHA-256 of the English page in `source_sha256`. When the English page changes, `check` prints the new hash. Translate the change, then set the new hash. The navigation titles and the site description come from the catalogs, with the context `docs`.

Build all three sites with:

```bash
uv sync --group docs
uv run python scripts/i18n.py build-docs
```

The plugin uses the QGIS interface language, from **Settings > Options > General**. To add a language, add its code to `LANGUAGES` in `core/i18n.py` and its digit separators to `_GROUP` and `_DECIMAL`. Then add it to `extra.alternate` in `mkdocs.yml`, run `update`, and translate every message and page.

## Release

1. List the changes under `## Unreleased` in `CHANGELOG.md`, as a flat list with no subheadings. qgis-plugin-ci copies the list into the changelog that the QGIS plugin manager shows, and it stops at the next heading.
2. Run `uv run cz bump`. It updates `pyproject.toml` and `metadata.txt`, moves the notes under a `## <version> - <date>` heading, then tags the commit. The bump stops when the Unreleased notes are empty.
3. Push the commit and the tag, then publish a GitHub release from the tag.
4. `.github/workflows/release.yml` packages the plugin with `qgis-plugin-ci`, attaches the zip to the release, and uploads it to the official QGIS plugin repository.
