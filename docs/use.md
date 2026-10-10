# Use

Open the panel from **Web > Portolan Registry** or the toolbar button.

## Find a catalog

The panel opens on the registry's catalogs. Each row shows a small map with the catalog's extent, its collection count, size, and licenses, and its logo when the registry has one. A catalog whose extent covers most of the world tints the land instead of drawing a box. Filter the list by text, by status, or to catalogs that overlap the map extent.

The list shows 25 catalogs at a time. Select **Show 25 more** at the end of the list to see the next ones.

Select a catalog to open it. The header shows the catalog, and the tree below lists its collections. Each collection shows its own icon when the catalog publishes one. The tree lists 50 entries at a time, with a **Show 50 more** row for the rest. **All catalogs** goes back to the list.

Select a node to see its thumbnail, or a map of its extent when it has no thumbnail. The details also show its description, license, and assets. Select the header to see the catalog's own details again.

## Add data to the map

Select one or more assets, then select **Add to map**. When a collection opens, the panel selects one asset for you, so **Add to map** works at once. It picks the first PMTiles link, which is the archive that the default style draws. Without PMTiles, it picks the first data asset in this order: COG, COPC, GeoParquet, FlatGeobuf, GeoJSON. It skips GeoParquet when DuckDB is missing. A double-click on an asset adds it too. A badge on each asset shows its format.

| Asset | Layer |
|---|---|
| PMTiles link or asset | Vector tile layer, with the collection's MapLibre styles |
| GeoParquet | Vector layer from a GeoPackage that DuckDB writes |
| COG | Raster layer, read over HTTP |
| GeoJSON, FlatGeobuf | Vector layer, read over HTTP |

A PMTiles layer gets every MapLibre style in the collection that draws its archive, each as a named QGIS style. The collection's default style is current. To switch styles, right-click the layer, then select one under **Styles**. **QGIS default style** draws the tiles without a catalog style. A project saves every style with the layer.

In a style, `sources.<id>.url` references the archive. The plugin accepts a path relative to the style file, such as `../tiles.pmtiles`. The `pmtiles://` prefix is optional. A vector source with no `url` reads the collection's archive when the collection has only one.

Add the PMTiles to look at the data. They draw at every zoom with the catalog's style, and they read only the tiles on screen. Load the GeoParquet to analyze the data. It gives every feature with its full attributes, so the attribute table, selections, and processing tools work on it.

DuckDB copies the GeoParquet features into a GeoPackage in the QGIS profile folder, and the layer reads that file. **Read GeoParquet in the map extent only** copies only the features that intersect the map extent. When a file has a GeoParquet `bbox` column, DuckDB uses its statistics to skip row groups outside the extent. Clear the box to copy the whole file.

Loads have no feature limit. For an estimate of more than 1,000,000 features, the panel asks first, because the copy takes time and disk space. To stop a read or a copy, select **Cancel** in the QGIS task manager.

A layer stores one geometry type. When a feature has another type, it keeps its attributes and has no geometry, and the panel reports how many features this affects.

The plugin deletes a GeoPackage when you remove the last layer that reads it. A saved project keeps each GeoParquet layer as a link to its source file and its extent. When the project opens, the plugin copies the features again. The new copy drops your edits, so export the layer to keep them.

PMTiles layers read through a loopback server that the plugin runs on `127.0.0.1`. A saved project reconnects them when it reopens.

## Download

**Download > Selected assets** saves the selected assets. **Download > Everything in** saves the chosen node and everything below it, after it shows the file count and the known size.

The plugin saves each file in the folder you pick, at the same relative path as in the catalog, so the copy opens as a local STAC catalog. After each download, the plugin checks the file against its `file:checksum` when the catalog gives one. A second run skips files that are already complete, so it resumes an interrupted download.

## Use another registry

The plugin reads the Portolan registry export at `exports/catalogs.json`. To point it at a mirror, set the QGIS setting `PortolanRegistry/registry_url` in **Settings > Options > Advanced** and reopen the panel.
