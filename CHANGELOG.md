# Changelog

## Unreleased

- The registry opens in a window instead of a dock. The window keeps its size, its divider, and the open state of its sections between sessions.
- The window header shows the Portolan mark, a refresh button, and **Open in browser**, which opens the chosen node in the Portolan browser.
- The details show in four sections that you can close: **About**, **Add to map**, **All files**, and **Details**.
- The hero shows the thumbnail, or the extent map when there is no thumbnail, not both.
- The status filter is gone. A catalog that is not valid still shows its status in its row.
- The details name the publisher, the license, and the total size. They no longer show the STAC type, the ID, or the extent coordinates.
- **View on map** adds the PMTiles, else a COG. **Load full data** adds the GeoParquet, else the FlatGeobuf, GeoJSON, or CSV. Their tooltips say which to use.
- **All files** lists only the files QGIS can open, and lists a PMTiles archive once when it is both a link and an asset.
- A check mark shows on each file that a layer in the project already reads.
- **Details** is one list of facts: publisher, license, size, documentation, source, and a link to the browser. Each link shows once, named by its host when its title only repeats the row name. **Source** is the `via` page the data came from. The raw STAC document link is gone.
- **All files** leaves out assets with the `archive` role, such as earlier extracts. The browser lists them.
- Right-click a publisher to open its catalog in the browser. The QGIS setting `PortolanRegistry/browser_url` points the links at a browser mirror.
- CSV files open over HTTP, as points when the header names longitude and latitude columns.
- A Parquet file with no geometry column loads as a table.
- A Parquet file whose `table:columns` name no geometry column shows as **Parquet**, not **GeoParquet**. Its button reads **Load table**, and the area picker hides.
- An area picker replaces **Read GeoParquet in the map extent only**. It offers everything, the map view, a drawn polygon, or the polygons of a layer.
- A GeoParquet load with a polygon keeps only the features that intersect the polygon. A saved project copies the same area again.
- A collection whose children are all items shows one **N files** row in the tree.
- In such a collection, **All files** lists one row for every file of the items, then the COG file of each item, one page at a time.
- **Add as one layer** adds the COG files of a collection's items in the area as one VRT mosaic. A saved project builds the VRT again.
- **Download > Everything in** leaves out the items outside the area.
- When a new layer lands outside the map, the map moves to it, and the message says so. Before, the layer showed in the Layers panel only.
- A COG that failed to open once opens on the next try. GDAL kept a failed request for the life of the QGIS session. COG reads now retry HTTP 429, 500, 502, 503, and 504.
- When a mosaic leaves a file out, the message log gives GDAL's reason, such as an HTTP status.
- With a map view or an area, **Download > Selected files** saves a GeoParquet file as a GeoPackage of the features in the area.
- GeoParquet loads through a GeoPackage that DuckDB writes. A million polygons from a local file load in 2 seconds instead of 19, QGIS stays responsive, and memory use drops from 1.9 GB to 380 MB.
- GeoParquet files with date or timestamp columns load their features. Before, the layer was empty.
- A GeoParquet load has no feature limit. When it holds more than 1,000,000 features, the panel asks first.
- A saved project copies its GeoParquet layers again when it opens.
- On QGIS 3.34, a saved GeoParquet layer opens as unavailable until the plugin copies it again. QGIS 3.34 crashes when a plugin registers a path preprocessor, so the plugin does not register one there.
- A GeoParquet feature whose geometry type does not fit the layer keeps its attributes, with no geometry. The panel reports the count.
- A GeoParquet attribute named `geom` loads. Before, the copy dropped it.
- A collection opens with one file selected, so **Add selected** works at once. PMTiles come first.
- PMTiles layers carry each of the collection's MapLibre styles as a named QGIS style. Switch styles from the layer's **Styles** menu.
- Styles that name their archive as a bare relative path, such as `../tiles.pmtiles`, apply to the tiles. So do styles whose vector source has no `url`.
- A PMTiles layer opens the archive you selected, not the archive the chosen style reads.
- PMTiles layers draw faster when you pan. The plugin keeps its connections to the archive's host open.
- Within one QGIS session, an area of a PMTiles layer that you saw before draws from the QGIS cache without a download.
- QGIS now stores PMTiles tiles in its disk cache, as it does for other tile layers. A server that sends `Cache-Control: no-store` keeps its tiles out of the cache.
- A PMTiles layer opens with one request fewer when the archive's metadata is in its first 16 KiB. Adding the same archive again keeps the tiles QGIS cached.
- On QGIS 3.34 and 3.36, a PMTiles archive behind a redirect reads only the bytes it needs. Before, each read downloaded the whole archive. On those versions, a relative redirect works too.
- The plugin no longer reads PMTiles from a server that ignores HTTP range requests on QGIS 3.34 and 3.36. Before, a small archive worked, and each tile read downloaded the whole archive. QGIS 3.38 and later already refused such servers.
- COG, GeoJSON, and FlatGeobuf assets open with no directory listing and no side-car probes. GDAL requests only the asset itself. GDAL now ignores external side-car files such as `.ovr`, `.aux.xml`, and `.msk` next to an asset.
- **Download all** reads eight catalog documents at the same time while it lists a catalog.
- Downloads run three files at the same time. A checksum check no longer holds up the next download.
- A download reports a failure for each file whose local path an earlier file already uses, such as `Data.tif` and `data.tif`.
- A cancelled **Download all** listing no longer asks to download the partial list.

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
