---
source_sha256: 3729ac4afbd76aebdbed0f771bdaa436e409ad75fec10bee41d9ff837414c464
---

<!-- vale off -->

# Installation

L'extension n'est pas encore dans le dépôt officiel des extensions QGIS. Installez-la depuis un zip de version ou depuis un clone.

## Depuis une version publiée

Téléchargez le zip depuis la [page des versions](https://github.com/portolan-sdi/portolan-registry-qgis/releases). Dans QGIS, ouvrez **Extensions > Installer/Gérer les extensions > Installer depuis un ZIP** et choisissez le fichier.

## Depuis un clone

Liez le dossier du paquet dans votre profil QGIS, puis activez **Portolan Registry** dans le gestionnaire d'extensions.

```bash
git clone https://github.com/portolan-sdi/portolan-registry-qgis
PLUGINS=~/.local/share/QGIS/QGIS3/profiles/default/python/plugins
ln -s "$PWD/portolan-registry-qgis/portolan_registry_qgis" \
  "$PLUGINS/portolan_registry_qgis"
```

Un QGIS en Flatpak garde son profil sous `~/.var/app/org.qgis.qgis/data/QGIS/QGIS3/profiles/default/python/plugins`.

## DuckDB pour le GeoParquet

QGIS n'inclut pas DuckDB. L'extension fonctionne sans, et le demande la première fois que vous ajoutez une ressource GeoParquet.

Installez-le avec le Python qu'utilise QGIS, puis redémarrez QGIS. Sous Linux avec un QGIS du système :

```bash
python3 -m pip install --user 'duckdb>=1.5.0'
```

Avec le QGIS en Flatpak, le Python du bac à sable est en lecture seule. Installez donc dans votre dossier utilisateur :

```bash
flatpak run --command=python3 org.qgis.qgis \
  -m pip install --user 'duckdb>=1.5.0'
```

Sous Windows et macOS, exécutez ceci dans la console Python de QGIS :

```python
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "duckdb>=1.5.0"])
```

DuckDB télécharge ses extensions `httpfs` et `spatial` à la première utilisation.

## Codecs raster

Le GDAL de la version Flatpak de QGIS n'a pas le codec LERC. Les COG compressés en `LERC`, `LERC_DEFLATE` ou `LERC_ZSTD`, comme les grilles de population GHSL, ne s'y ouvrent pas, et l'extension signale le codec manquant. Pour vérifier un fichier, exécutez `gdalinfo` avec un GDAL qui a LERC et lisez `COMPRESSION` dans les métadonnées de structure de l'image. Ouvrez ces fichiers dans un QGIS de votre distribution Linux, de conda-forge ou des installateurs QGIS.
