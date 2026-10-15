---
source_sha256: c2fe4741641d76cda598415a7e7cb22bc3f4138244a3b7082762da4258456484
---

<!-- vale off -->

# Desarrollo

El complemento tiene dos conjuntos de pruebas.

- `tests/core` cubre la lógica de STAC, del registro, de PMTiles, de GeoParquet y de descarga en `portolan_registry_qgis/core`. Ese paquete no importa ni QGIS ni GDAL, así que el conjunto se ejecuta en el entorno de uv.
- `tests/in_qgis` necesita PyQGIS. Crea un catálogo pequeño con GDAL y DuckDB y lo sirve en la interfaz de bucle local. Después, las pruebas ejercitan el código de red, las capas, las descargas y la ventana.

```bash
uv sync
uv run pytest
```

Para el conjunto de QGIS, cree un entorno virtual sobre el Python que tiene PyQGIS:

```bash
uv venv --python /usr/bin/python3 --system-site-packages .venv-qgis
VIRTUAL_ENV=.venv-qgis uv pip install pytest pytest-timeout duckdb babel pytest-cov
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python -m pytest tests/in_qgis
```

Añada `-m network` para ejecutar las comprobaciones en vivo contra el registro y dos catálogos publicados.

La CI ejecuta `tests/in_qgis` en las imágenes Docker oficiales de QGIS 3.34, 3.44 y 4.2. QGIS 3.34 es la versión más antigua que admite el complemento, y 4.2 cubre Qt6.

Para medir el tiempo de renderizado de PMTiles con un archivo remoto, ejecute la prueba de rendimiento con el mismo Python. La opción `--baseline` restaura el transporte de teselas de la versión 0.1.1 para comparar.

```bash
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python \
  scripts/bench_tiles.py
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python \
  scripts/bench_tiles.py --baseline
```

## Barridos del registro

Las pruebas usan catálogos que crea el complemento. Los scripts de barrido, en cambio, ejecutan el complemento contra cada catálogo del registro en vivo e informan de cada comprobación que falla.

- `scripts/sweep_registry.py` ejecuta la ruta de lectura del núcleo con uv. Para cada colección, prueba los archivos que hay detrás de **Ver en el mapa** y **Cargar datos completos**, y planifica la lectura del GeoParquet con DuckDB. También lee los estilos, la miniatura y las rutas de descarga.
- `scripts/sweep_qgis.py` añade los destinos de cada colección como capas de QGIS con sus estilos y luego renderiza cada capa. Informa de las capas no válidas, los renderizados en blanco y las advertencias de estilo.

```bash
uv run python scripts/sweep_registry.py --out sweep
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python \
  scripts/sweep_qgis.py --out sweep-qgis
```

Cada script escribe `sweep.md` y `sweep.json` en su carpeta de salida. Use `--only` con identificadores de catálogo para barrer unos pocos catálogos. El flujo de trabajo Registry sweep ejecuta los dos scripts cada noche y sube los informes. Un fallo ahí no bloquea una fusión.

## Controles de calidad

Pre-commit ejecuta estas comprobaciones, y la CI las vuelve a ejecutar en cada pull request. Un cambio debe superar cada una.

| Control | Umbral |
| --- | --- |
| Ruff | El conjunto de reglas de `pyproject.toml`, con las reglas de pylint, pytest y tryceratops. Complejidad de McCabe de 10 o menos. |
| mypy | Modo estricto en el paquete y en `scripts`. |
| vulture | Ningún nombre sin usar con una confianza del 60 % o más. |
| xenon en el paquete | Cada bloque en el rango B o mejor. Cada módulo y la media en el rango A. |
| xenon en las pruebas y los scripts | Cada bloque en el rango C o mejor. Cada módulo en el rango B o mejor, y la media en el rango A. |
| jscpd | Ninguna copia de 50 tokens y 5 líneas o más. |
| Docstrings | 5 líneas o menos. Ningún historial de cambios en un docstring o un comentario. |
| import-linter | El núcleo no importa código de QGIS, Qt ni GDAL, ni ninguna de las capas de QGIS. `qgis_io` no importa nada de `gui`. |
| Cobertura, conjunto de uv | El 99 % del núcleo. |
| Cobertura, conjunto de QGIS | El 87 % de `plugin.py`, `gui` y `qgis_io`. |
| diff-cover | El 90 % de las líneas modificadas. Solo en la CI. |

Ejecute los controles uno a uno:

```bash
uv run ruff check .
uv run mypy
uv run vulture
uv run xenon --max-absolute=B --max-modules=A \
  --max-average=A portolan_registry_qgis
uv run xenon --max-absolute=C --max-modules=B \
  --max-average=A tests scripts
uvx prek run jscpd --all-files
uv run python scripts/docstring_check.py
uv run lint-imports
uv run pytest -n 4 --cov=portolan_registry_qgis
```

El conjunto de QGIS toma su configuración de cobertura de un archivo propio:

```bash
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python \
  -m pytest -p no:cacheprovider tests/in_qgis \
  --cov=portolan_registry_qgis \
  --cov-config=tests/in_qgis/coverage.ini \
  --cov-report=term-missing --cov-fail-under=87
```

Para ejecutar todos los hooks de las dos etapas:

```bash
uvx prek run --all-files
uvx prek run --all-files --hook-stage pre-push
```

## Traducciones

El complemento y este sitio están en inglés, español y francés. El inglés es el idioma de origen, y un cambio en un texto en inglés necesita las traducciones al español y al francés en la misma pull request. Pre-commit y la CI ejecutan `scripts/i18n.py check`, que falla hasta que los dos idiomas estén al día.

El complemento traduce su texto con gettext. Envuelva cada cadena que lee una persona en `_()` de `portolan_registry_qgis.core.i18n`, y use `count()` para un texto con un número:

```python
from portolan_registry_qgis.core.i18n import _, count

label.setText(_("Zoom to"))
status.setText(_("Could not open {url}").format(url=url))
row.setText(count("{n} file", "{n} files", total))
```

Como el complemento elige el idioma después de que Python importa sus módulos, una constante de módulo almacena su texto en inglés dentro de `N_()`, y el código que la muestra llama a `_()`. Los marcadores de posición tienen nombre, lo que permite que una traducción los reordene. Babel copia un comentario que empieza por `# Translators:`, en la línea anterior a una llamada, al catálogo junto al mensaje.

Cuando cambie un texto en inglés, regenere los catálogos y traduzca las entradas nuevas:

```bash
uv run python scripts/i18n.py update
uv run python scripts/i18n.py check
```

`update` extrae el inglés a `portolan_registry_qgis/locale/messages.pot` y lo combina con el `messages.po` de cada idioma. Una cadena nueva llega sin traducción. Una cadena modificada llega marcada como `#, fuzzy`, con su traducción anterior. Escriba la traducción, quite la marca `fuzzy` y luego ejecute `update` de nuevo. Esa ejecución compila `messages.mo` y escribe las líneas `description[es]` y `about[es]` de `metadata.txt`, que muestra el administrador de complementos de QGIS.

Cada página de `docs/` tiene una traducción en `docs-i18n/<lang>/` con la misma ruta. Su front matter registra el SHA-256 de la página en inglés en `source_sha256`. Cuando la página en inglés cambia, `check` muestra el hash nuevo. Traduzca el cambio y luego defina el hash nuevo. Los títulos de navegación y la descripción del sitio vienen de los catálogos, con el contexto `docs`.

Construya los tres sitios con:

```bash
uv sync --group docs
uv run python scripts/i18n.py build-docs
```

El complemento usa el idioma de la interfaz de QGIS, de **Configuración > Opciones > General**. Para añadir un idioma, añada su código a `LANGUAGES` en `core/i18n.py` y sus separadores de dígitos a `_GROUP` y `_DECIMAL`. Después añádalo a `extra.alternate` en `mkdocs.yml`, ejecute `update` y traduzca todos los mensajes y páginas.

## Publicación

1. Liste los cambios bajo `## Unreleased` en `CHANGELOG.md`, como una lista plana sin subtítulos. qgis-plugin-ci copia la lista en el registro de cambios que muestra el administrador de complementos de QGIS, y se detiene en el siguiente encabezado.
2. Ejecute `uv run cz bump`. Actualiza `pyproject.toml` y `metadata.txt`, mueve las notas bajo un encabezado `## <version> - <date>` y luego etiqueta el commit. El bump se detiene cuando las notas de Unreleased están vacías.
3. Envíe el commit y la etiqueta, y luego publique una versión de GitHub a partir de la etiqueta.
4. `.github/workflows/release.yml` empaqueta el complemento con `qgis-plugin-ci`, adjunta el zip a la versión y lo sube al repositorio oficial de complementos de QGIS.
