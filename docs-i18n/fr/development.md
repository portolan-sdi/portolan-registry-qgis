---
source_sha256: aeda9e0355eda095003303e738695ee4e07113745adbbc096e1702c9c6ab20b6
---

<!-- vale off -->

# Développement

L'extension a deux suites de tests.

- `tests/core` couvre la logique STAC, registre, PMTiles, GeoParquet et téléchargement de `portolan_registry_qgis/core`. Ce paquet n'importe ni QGIS ni GDAL, donc la suite s'exécute dans l'environnement uv.
- `tests/in_qgis` nécessite PyQGIS. Elle construit un petit catalogue avec GDAL et DuckDB et le sert en boucle locale. Les tests exercent ensuite le code réseau, les couches, les téléchargements et la fenêtre.

```bash
uv sync
uv run pytest
```

Pour la suite QGIS, créez un environnement virtuel sur le Python qui a PyQGIS :

```bash
uv venv --python /usr/bin/python3 --system-site-packages .venv-qgis
VIRTUAL_ENV=.venv-qgis uv pip install pytest pytest-timeout duckdb babel
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python -m pytest tests/in_qgis
```

Ajoutez `-m network` pour exécuter les vérifications en direct sur le registre et deux catalogues publiés.

La CI exécute `tests/in_qgis` dans les images Docker officielles de QGIS 3.34, 3.44 et 4.2. QGIS 3.34 est la plus ancienne version que l'extension prend en charge, et 4.2 couvre Qt6.

Pour mesurer le temps de rendu PMTiles sur une archive distante, exécutez le banc d'essai avec le même Python. L'option `--baseline` rétablit le transport de tuiles de la version 0.1.1 pour comparaison.

```bash
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python \
  scripts/bench_tiles.py
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python \
  scripts/bench_tiles.py --baseline
```

## Traductions

L'extension et ce site sont en anglais, en espagnol et en français. L'anglais est la langue source, et une modification d'un texte anglais nécessite les traductions espagnole et française dans la même pull request. Pre-commit et la CI exécutent `scripts/i18n.py check`, qui échoue tant que les deux langues ne sont pas à jour.

L'extension traduit son texte avec gettext. Entourez chaque chaîne qu'une personne lit avec `_()` de `portolan_registry_qgis.core.i18n`, et utilisez `count()` pour un texte avec un nombre :

```python
from portolan_registry_qgis.core.i18n import _, count

label.setText(_("Zoom to"))
status.setText(_("Could not open {url}").format(url=url))
row.setText(count("{n} file", "{n} files", total))
```

Comme l'extension choisit la langue après que Python a importé ses modules, une constante de module stocke son texte anglais dans `N_()`, et le code qui l'affiche appelle `_()`. Les champs de substitution portent des noms, ce qui permet à une traduction de les réordonner. Babel copie un commentaire qui commence par `# Translators:`, sur la ligne au-dessus d'un appel, dans le catalogue à côté du message.

Quand un texte anglais change, régénérez les catalogues et traduisez les nouvelles entrées :

```bash
uv run python scripts/i18n.py update
uv run python scripts/i18n.py check
```

`update` extrait l'anglais dans `portolan_registry_qgis/locale/messages.pot` et le fusionne dans le `messages.po` de chaque langue. Une nouvelle chaîne arrive sans traduction. Une chaîne modifiée arrive marquée `#, fuzzy` avec son ancienne traduction. Écrivez la traduction, retirez le marqueur `fuzzy`, puis exécutez à nouveau `update`. Cette exécution compile `messages.mo` et écrit les lignes `description[es]` et `about[es]` de `metadata.txt`, que le gestionnaire d'extensions de QGIS affiche.

Chaque page de `docs/` a une traduction dans `docs-i18n/<lang>/` au même chemin. Son front matter enregistre le SHA-256 de la page anglaise dans `source_sha256`. Quand la page anglaise change, `check` affiche le nouveau hash. Traduisez la modification, puis définissez le nouveau hash. Les titres de navigation et la description du site viennent des catalogues, avec le contexte `docs`.

Construisez les trois sites avec :

```bash
uv sync --group docs
uv run python scripts/i18n.py build-docs
```

L'extension utilise la langue de l'interface de QGIS, définie dans **Préférences > Options > Général**. Pour ajouter une langue, ajoutez son code à `LANGUAGES` dans `core/i18n.py` et ses séparateurs de chiffres à `_GROUP` et `_DECIMAL`. Ajoutez-la ensuite à `extra.alternate` dans `mkdocs.yml`, exécutez `update`, et traduisez chaque message et chaque page.

## Publication

1. Listez les modifications sous `## Unreleased` dans `CHANGELOG.md`, en liste simple sans sous-titres. qgis-plugin-ci copie la liste dans le journal des modifications qu'affiche le gestionnaire d'extensions de QGIS, et s'arrête au titre suivant.
2. Exécutez `uv run cz bump`. La commande met à jour `pyproject.toml` et `metadata.txt`, déplace les notes sous un titre `## <version> - <date>`, puis étiquette le commit. Le bump s'arrête quand les notes Unreleased sont vides.
3. Poussez le commit et l'étiquette, puis publiez une release GitHub depuis l'étiquette.
4. `.github/workflows/release.yml` empaquette l'extension avec `qgis-plugin-ci`, joint le zip à la release et le téléverse dans le dépôt officiel des extensions QGIS.
