---
source_sha256: dc3272a7df0a277b0cebb474761df61ff2cad5c81c359b4f9a0e59a073ebece7
---

<!-- vale off -->

# Instalación

## Desde el administrador de complementos

1. En QGIS, abra **Complementos > Administrar e instalar complementos…**.
2. Busque **Portolan Registry** y selecciónelo.
3. Seleccione **Instalar complemento**.

## Desde una versión publicada

Descargue el zip de la [página de versiones](https://github.com/portolan-sdi/portolan-registry-qgis/releases). En QGIS, abra **Complementos > Administrar e instalar complementos > Instalar a partir de ZIP** y seleccione el archivo.

## Desde un clon

Enlace la carpeta del paquete en su perfil de QGIS y luego active **Portolan Registry** en el administrador de complementos.

```bash
git clone https://github.com/portolan-sdi/portolan-registry-qgis
PLUGINS=~/.local/share/QGIS/QGIS3/profiles/default/python/plugins
ln -s "$PWD/portolan-registry-qgis/portolan_registry_qgis" \
  "$PLUGINS/portolan_registry_qgis"
```

Un QGIS de Flatpak guarda su perfil en `~/.var/app/org.qgis.qgis/data/QGIS/QGIS3/profiles/default/python/plugins`.

## DuckDB para GeoParquet

QGIS no incluye DuckDB. El complemento funciona sin él, y lo pide la primera vez que usted añade un recurso GeoParquet.

Instálelo con el Python que usa QGIS y luego reinicie QGIS. En Linux con un QGIS del sistema:

```bash
python3 -m pip install --user 'duckdb>=1.5.0'
```

Con el QGIS de Flatpak, el Python del entorno aislado es de solo lectura, así que instálelo en su carpeta de usuario:

```bash
flatpak run --command=python3 org.qgis.qgis \
  -m pip install --user 'duckdb>=1.5.0'
```

En Windows y macOS, ejecute esto en la consola de Python de QGIS:

```python
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "duckdb>=1.5.0"])
```

DuckDB descarga sus extensiones `httpfs` y `spatial` la primera vez que las usa.

## Códecs ráster

El GDAL de la compilación Flatpak de QGIS no tiene el códec LERC. Los COG comprimidos con `LERC`, `LERC_DEFLATE` o `LERC_ZSTD`, como las cuadrículas de población de GHSL, no se abren ahí, y el complemento informa del códec que falta. Para comprobar un archivo, ejecute `gdalinfo` con un GDAL que tenga LERC y lea `COMPRESSION` en los metadatos de estructura de la imagen. Abra esos archivos en un QGIS de su distribución de Linux, de conda-forge o de los instaladores de QGIS.
