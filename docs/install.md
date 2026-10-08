# Install

The plugin is not in the official QGIS plugin repository yet. Install it from a release zip or from a clone.

## From a release

Download the zip from the [releases page](https://github.com/portolan-sdi/portolan-registry-qgis/releases). In QGIS, open **Plugins > Manage and Install Plugins > Install from ZIP** and pick the file.

Use the plugin ZIP attached to a release. GitHub's **Source code** archives
contain the repository above the plugin folder, so QGIS cannot install them.
The plugin ZIP contains `portolan-python`; you do not need to install that
library in QGIS.

## From a clone

Link the package folder into your QGIS profile, then enable **Portolan Registry** in the plugin manager.

```bash
git clone https://github.com/portolan-sdi/portolan-registry-qgis
PLUGINS=~/.local/share/QGIS/QGIS3/profiles/default/python/plugins
ln -s "$PWD/portolan-registry-qgis/portolan_registry_qgis" \
  "$PLUGINS/portolan_registry_qgis"
```

A Flatpak QGIS keeps its profile under `~/.var/app/org.qgis.qgis/data/QGIS/QGIS3/profiles/default/python/plugins`.

## DuckDB for GeoParquet

QGIS does not include DuckDB. The plugin works without it, and asks for it the first time you add a GeoParquet asset.

Install it with the Python that QGIS uses, then restart QGIS. On Linux with a system QGIS:

```bash
python3 -m pip install --user 'duckdb>=1.5.0'
```

With the Flatpak QGIS, the Python inside the sandbox is read-only, so install into your user folder:

```bash
flatpak run --command=python3 org.qgis.qgis \
  -m pip install --user 'duckdb>=1.5.0'
```

On Windows and macOS, run this in the QGIS Python console:

```python
import subprocess, sys

subprocess.check_call([sys.executable, "-m", "pip", "install", "duckdb>=1.5.0"])
```

DuckDB downloads its `httpfs` and `spatial` extensions on first use.

## Raster codecs

GDAL in the Flatpak build of QGIS lacks the LERC codec. COGs compressed with `LERC`, `LERC_DEFLATE`, or `LERC_ZSTD`, such as the GHSL population grids, fail to open there, and the plugin reports the missing codec. To check a file, run `gdalinfo` with a GDAL that has LERC and read `COMPRESSION` under the image structure metadata. Open such files in a QGIS from your Linux distribution, conda-forge, or the QGIS installers.
