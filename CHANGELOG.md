# Changelog

## Unreleased

- **Zoom to Layer** on a PMTiles layer frames the data instead of the whole world. The layer properties show the extent of the archive.
- The plugin shows its window, messages, and plugin manager description in Spanish and French when QGIS runs in either language. Numbers and file sizes use that language's separators.
- The documentation site is available in Spanish at `/es/` and in French at `/fr/`, with a language menu in the header.
- **Load full data** finds the geometry of a GeoParquet file without geo metadata in a WKB column named `shape` or `geog`, the same names that the STAC table columns use.
- A PMTiles style or sprite on an S3 website endpoint loads over HTTPS, as the catalog itself does.
- **Cancel** stops the search for the files of a mosaic, the build of the mosaic, and the listing of the files to download. Before, it stopped only a download. The progress bar shows while the mosaic builds.

## 0.2.0 - 2026-10-11

- The plugin is no longer marked experimental, so the QGIS plugin manager lists it without the experimental option.
- The registry opens in a window instead of a side panel. You can pan the map while it is open. It keeps its size, its divider, and the open state of its sections between sessions.
- The details pane shows the dataset's name with **Zoom to** and **Open in Portolan browser ↗** next to it. The browser shows every file and all the metadata that the plugin leaves out.
- The details show in four sections that you can close: **About**, **Add to map**, **All files**, and **Details**. **All files** starts closed.
- **About** shows the thumbnail, or a map of the extent when there is no thumbnail.
- **Details** is one list: publisher, license, size, documentation, source, and a link to the browser. Each link shows once. A license value of `other` shows as **Custom terms**.
- The status filter and the STAC type, ID, and extent coordinates are gone from the window.
- **Add to map** starts with the area of interest: everything, the map extent, a drawn polygon, or the polygons of a layer.
- **View on map** adds the PMTiles, else a COG. **Load full data** adds the GeoParquet, else the FlatGeobuf, GeoJSON, or CSV. Each tooltip names the format first.
- **Load full data** copies only the GeoParquet features that intersect the area of interest. A feature that crosses the edge of the area stays whole. A saved project copies the same area again.
- A collection of COG items shows as one **N files** row in the tree. **Add as one layer** adds the COGs in the area of interest as one layer, clipped to the area. A saved project builds the layer again.
- **Download** saves everything in the area of interest. It leaves out items outside the area, and it saves each GeoParquet file as a GeoPackage of the features in the area.
- **All files** lists only the files QGIS can open, each one once. It leaves out styles, thumbnails, upstream `source` files, earlier `archive` extracts, and item indexes. **Add selected** and **Download selected** act on the selected files, whole.
- A check mark shows on each file that a layer in the project already reads.
- CSV files open over HTTP, as points when the header names longitude and latitude columns in degrees.
- A Parquet file with no geometry column shows as **Parquet** and loads as a table with **Load table**.
- When a new layer lands outside the map, the map moves to it.
- Right-click a publisher to open its catalog in the browser. The QGIS setting `PortolanRegistry/browser_url` points the links at a browser mirror.
- A click that must wait for a running task says so. An error after a task ends goes to the QGIS message log.
- COGs open when the GDAL setting `CPL_VSIL_CURL_ALLOWED_EXTENSIONS` is set. A file that the setting blocks gets a message that names the setting.
- A COG that failed to open once opens on the next try. COG reads retry HTTP 429, 500, 502, 503, and 504. When GDAL cannot read a file, the message gives the reason, such as an HTTP status.
- COG, GeoJSON, and FlatGeobuf files open with no directory listing and no side-car probes. GDAL ignores external side-car files such as `.ovr` and `.aux.xml`.
- GeoParquet loads through a GeoPackage that DuckDB writes. A million polygons from a local file load in 2 seconds instead of 19, QGIS stays responsive, and memory use drops from 1.9 GB to 380 MB.
- A GeoParquet load has no feature limit. Above 1,000,000 features, the window asks first.
- GeoParquet files with date or timestamp columns load their features, and an attribute named `geom` loads. A feature whose geometry type does not fit the layer keeps its attributes, and the window reports the count.
- A saved project copies its GeoParquet layers again when it opens. On QGIS 3.34, such a layer shows as unavailable until the copy ends.
- PMTiles layers carry each of the collection's MapLibre styles as a named QGIS style. Switch styles from the layer's **Styles** menu. A PMTiles layer opens the archive you selected.
- Styles that name their archive as a relative path, such as `../tiles.pmtiles`, apply to the tiles. So do styles whose vector source has no `url`.
- PMTiles layers draw faster when you pan, and QGIS keeps their tiles in its disk cache. A server that sends `Cache-Control: no-store` keeps its tiles out of the cache.
- On QGIS 3.34 and 3.36, a PMTiles archive behind a redirect reads only the bytes it needs, and a server that ignores HTTP range requests is refused.
- Downloads list eight catalog documents and fetch three files at the same time. A cancelled listing asks nothing. A file whose local path an earlier file already uses, such as `Data.tif` and `data.tif`, is reported as a failure.

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
