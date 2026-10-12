---
source_sha256: 5e2ac3299c3cc7687d86698df36b0bd3c4f74a5a32017b29340aa78dd6289043
---

<!-- vale off -->

# Complemento del registro de Portolan para QGIS

Un complemento de QGIS para el [registro de Portolan](https://github.com/portolan-sdi/portolan-registry). Lista todos los catálogos registrados y recorre el árbol STAC de cada catálogo. Desde ahí, usted añade los datos al mapa o los descarga.

- **PMTiles** se convierten en capas de teselas vectoriales, dibujadas con el propio estilo MapLibre de la colección.
- **GeoParquet** se carga mediante [DuckDB](https://duckdb.org/). La vista del mapa, un polígono dibujado o una capa de polígonos pueden limitar la copia.
- **COG**, **GeoJSON**, **FlatGeobuf** y **CSV** se abren por HTTP sin descarga.
- **Las colecciones de ítems COG** se añaden como una capa de mosaico con los ítems del área elegida.
- **Las descargas** conservan la estructura de carpetas del catálogo y verifican cada archivo con su `file:checksum`. Solo conservan los ítems y los objetos espaciales GeoParquet del área elegida.

El complemento necesita QGIS 3.34 o posterior, y funciona en QGIS 4. GeoParquet también necesita DuckDB 1.5.0 o posterior. Consulte [Instalación](install.md).
