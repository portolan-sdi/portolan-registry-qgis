# Portolan registry plugin for QGIS

A QGIS plugin for the [Portolan registry](https://github.com/portolan-sdi/portolan-registry). It lists every registered catalog and walks each catalog's STAC tree. From there you add the data to the map or download it.

- **PMTiles** become vector tile layers drawn with the collection's own MapLibre style.
- **GeoParquet** loads through [DuckDB](https://duckdb.org/). The map view, a drawn polygon, or a polygon layer can limit the copy.
- **COG**, **GeoJSON**, **FlatGeobuf**, and **CSV** open over HTTP without a download.
- **Collections of COG or GeoParquet items** add as one layer of the items in the chosen area.
- **Partitioned GeoParquet** in `s3://` or `gs://` buckets loads as one table.
- **Downloads** keep the catalog's folder layout and check each file against its `file:checksum`. They keep only the items and GeoParquet features in the chosen area.

The plugin needs QGIS 3.34 or newer, and it runs on QGIS 4. GeoParquet also needs DuckDB 1.5.0 or newer. See [Install](install.md).
