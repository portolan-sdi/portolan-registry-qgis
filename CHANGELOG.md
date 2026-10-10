# Changelog

## Unreleased

- GeoParquet loads through a GeoPackage that DuckDB writes. A million polygons from a local file load in 2 seconds instead of 19, QGIS stays responsive, and memory use drops from 1.9 GB to 380 MB.
- GeoParquet files with date or timestamp columns load their features. Before, the layer was empty.
- A GeoParquet load has no feature limit. When it holds more than 1,000,000 features, the panel asks first.
- Tooltips on **Add to map**, PMTiles, and GeoParquet say which to use: PMTiles to view, GeoParquet to analyze.
- A collection opens with one asset selected, so **Add to map** works at once. PMTiles come first.
- PMTiles layers carry each of the collection's MapLibre styles as a named QGIS style. Switch styles from the layer's **Styles** menu.
- Styles that name their archive as a bare relative path, such as `../tiles.pmtiles`, apply to the tiles. So do styles whose vector source has no `url`.
- A PMTiles layer opens the archive you selected, not the archive the chosen style reads.

## 0.1.1 - 2026-10-09

- Each GitHub release has a plugin ZIP that installs in QGIS.
- The QGIS plugin manager shows the changes in each release.

## 0.1.0 - 2026-10-07

- A layer that fails to open shows GDAL's reason. A missing codec, such as LERC in the QGIS Flatpak, names the codec and what to do.
- Registry panel with text, status, and map-extent filters. Each catalog shows a map of its extent and its logo.
- Catalog page with the catalog tree, collection icons, thumbnails, details, and an asset list with format badges.
- Pages of 25 catalogs and 50 tree entries.
- PMTiles vector tile layers styled with the collection's MapLibre style.
- GeoParquet layers through DuckDB, limited to the map extent by default.
- COG, GeoJSON, and FlatGeobuf layers over HTTP.
- Downloads that keep the catalog layout and verify `file:checksum`.
