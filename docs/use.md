# Use

Open the window from **Web > Portolan Registry** or the toolbar button. The window is not modal, so the map pans and zooms while it is open. It keeps its size and the position of its divider from one session to the next.

The refresh button in the header reads the registry again.

## Find a publisher

The left pane opens on the registry's publishers. Each row shows a small map with the catalog's extent, its collection count, size, and licenses, and its logo when the registry has one. A catalog whose extent covers most of the world tints the land instead of drawing a box. Filter the list by name or license, or select **In map extent** to keep the catalogs that overlap the map.

The list shows 25 publishers at a time. Select **Show 25 more** at the end of the list to see the next ones. Right-click a publisher and select **Open in Portolan browser** to see its catalog in the [Portolan browser](https://browser.portolan-sdi.org/).

Select a publisher to open its catalog. The header shows the catalog, and the tree below lists its collections. Each collection shows its own icon when the catalog publishes one. The tree lists 50 entries at a time, with a **Show 50 more** row for the rest. **All publishers** goes back to the list.

A collection whose children are all items, such as a set of image tiles, shows one row such as **240 files** instead of one row per item. Select the collection or that row, then use **Add as one layer** under **Add to map**. **All files** lists the data file of each item, such as a COG or a GeoParquet file, 50 at a time, with a **Show more** row. Select one to add or download that file alone.

## See the details

The right pane shows the name of the chosen node, with two buttons next to it. **Zoom to** moves the map to the node's extent. **Open in Portolan browser ↗** opens the node in the [Portolan browser](https://browser.portolan-sdi.org/), which shows every file and all the STAC metadata that the plugin leaves out.

Select a section's title to close or open it. The window remembers which sections you closed. **All files** starts closed.

- **About** shows the thumbnail, or a map of the extent when there is no thumbnail, and the description.
- **Add to map** holds the area of interest, then the buttons that add or download the data in it.
- **All files** lists the files QGIS can open, with **Add selected** and **Download selected**. A badge on each file shows its format, and a check mark shows that a layer in the project already reads it.
- **Details** is one list: **Publisher**, **License**, **Size**, **Documentation**, **Source**, and **More**, which links to the browser. The license, the documentation, and the source link to the publisher's pages. **Source** is the page the data came from, such as a city's open data portal. A license value of `other` shows as **Custom terms**, and a link that the catalog repeats shows once.

Select the catalog header to see the catalog's own details again.

## Add data to the map

**Add to map** shows the buttons that fit the node. **View on map** adds the PMTiles, or a COG when there are no PMTiles. Use it to look at the data, because it reads only the tiles on screen and draws with the publisher's style. **Load full data** adds the GeoParquet, else the FlatGeobuf, GeoJSON, or CSV. Use it for analysis, because it gives every feature with its full attributes. When the collection's `table:columns` name no geometry column, the badge says **Parquet** instead of **GeoParquet**, and the button reads **Load table**. Hold the pointer over a button to see the format it adds and the file.

To add a different file, open **All files**, select one or more rows, then select **Add selected**. A double-click on a row adds it too. **Add selected** adds each file whole, whatever the area of interest. **All files** lists each file once, and leaves out styles, thumbnails, upstream `source` files, earlier `archive` extracts, and the `collection-mirror` index.

| File | Layer |
|---|---|
| PMTiles link or asset | Vector tile layer, with the collection's MapLibre styles |
| GeoParquet | Vector layer from a GeoPackage that DuckDB writes |
| Parquet with no geometry | Table with no geometry, from a GeoPackage that DuckDB writes |
| COG | Raster layer, read over HTTP |
| GeoJSON, FlatGeobuf | Vector layer, read over HTTP |
| CSV | Point layer when the header names longitude and latitude columns, else a table, read over HTTP |

A PMTiles layer gets every MapLibre style in the collection that draws its archive, each as a named QGIS style. The collection's default style is current. To switch styles, right-click the layer, then select one under **Styles**. **QGIS default style** draws the tiles without a catalog style. A project saves every style with the layer.

In a style, `sources.<id>.url` references the archive. The plugin accepts a path relative to the style file, such as `../tiles.pmtiles`. The `pmtiles://` prefix is optional. A source with a `tiles` template such as `pmtiles://https://host/tiles.pmtiles/{z}/{x}/{y}` references the archive too. A vector source with no `url` reads the collection's archive when the collection has only one. When a style references the archive at another URL, the plugin matches the archive by its file name.

A CSV opens as points in EPSG:4326 when its first line has a longitude column (`lon`, `lng`, `longitude`, or `x`) and a latitude column (`lat`, `latitude`, or `y`). The names ignore case.

## Choose the area of interest

The area of interest at the top of **Add to map** limits the data that the buttons below it read. The choice shows when the node has GeoParquet or child nodes.

| Choice | Area |
|---|---|
| Everything | The whole dataset |
| Map extent | The rectangle the map shows when the load or download starts |
| Draw an area | A polygon that you click out on the map |
| From a layer | The polygons of a layer in the project, or its selected features only |

To draw an area, select **Draw an area**, then click the map to add each vertex. Right-click or double-click to finish the polygon after three or more vertices. Backspace removes the last vertex, and Esc cancels. The area stays on the map until you choose another area or close the window. **Draw again** replaces it.

When the area is not complete, the window says why under the choice and turns off the buttons that need it. **View on map** reads only the tiles on screen, so it does not use the area.

## Load GeoParquet

DuckDB copies the GeoParquet features into a GeoPackage in the QGIS profile folder, and the layer reads that file. With a map extent or an area, DuckDB copies only the features that intersect it. DuckDB copies a feature that crosses the edge of the area in full. When a file has a GeoParquet `bbox` column, DuckDB uses its statistics to skip row groups outside the area. A drawn or layer polygon then tests each feature against the polygon itself, not its bounding box.

The bar at the bottom of the window shows how much of the count and the copy DuckDB has done, and the time left. DuckDB reports its progress in steps, so the bar can wait at 0% while DuckDB reads the file metadata. **Cancel** stops the read or the copy.

Loads have no feature limit. For an estimate of more than 1,000,000 features, the window asks first, because the copy takes time and disk space. The estimate counts the features in the area's bounding box, so the copy can hold fewer. To stop a read or a copy, select **Cancel** in the QGIS task manager.

A layer stores one geometry type. When a feature has another type, it keeps its attributes and has no geometry, and the window reports how many features this affects. A Parquet file with no geometry column loads as a table with every row, whatever the area.

The plugin deletes a GeoPackage when you remove the last layer that reads it. A saved project keeps each GeoParquet layer as a link to its source file and its area. When the project opens, the plugin copies the features again. The new copy drops your edits, so export the layer to keep them.

## Load partitioned GeoParquet

A large dataset can split into many GeoParquet files, which the catalog describes with one glob such as `s3://bucket/data/country=*/*.parquet`. The plugin reads the glob from the collection's `partition:glob`, or from a data asset whose href holds `*`. DuckDB then reads every file as one table, and adds the partition keys, such as `country`, as columns.

HTTPS cannot list the files of a folder, so DuckDB needs the glob as an `s3://` or `gs://` URL. The plugin converts the HTTPS addresses of Amazon S3, Google Cloud Storage, and Source Cooperative to these forms. When the catalog gives only another HTTPS glob, **Load full data** is off, and its tooltip says why. Add single files from **All files** instead.

PMTiles layers read through a loopback server that the plugin runs on `127.0.0.1`. A saved project reconnects them when it reopens.

## Add the files of many items as one layer

For a collection whose children are all items, **Add to map** shows **Add as one layer**. The plugin finds the items that intersect the area of interest and reads the first COG of each item, else its GeoParquet file. DuckDB copies GeoParquet items into one GeoPackage, as **Load full data** does. For COG items, the plugin builds a GDAL VRT. With a map extent or an area, the VRT clips the mosaic to it, so the layer shows only the pixels inside. The clip keeps the CRS and the pixels of the COGs. QGIS adds the VRT as one raster layer and reads only the tiles on screen.

While GDAL opens the COGs, the bar shows the share of files opened and the time left.

When the collection has a `collection-mirror` asset in stac-geoparquet, DuckDB reads the item extents from it in one query. Without it, or without DuckDB, the plugin reads each item document. Above 500 files, the window asks first, because GDAL opens each file to build the VRT.

The VRT goes into the same scratch folder as the GeoPackages, and the plugin deletes it with the last layer that reads it. A saved project keeps the COG URLs and the clip, and the plugin builds the VRT again when the project opens.

## Download

**Download** under **Add to map** saves the chosen node and everything below it in the area of interest, after it shows the file count and the known size. It leaves out each item whose extent lies outside the area, and it says how many it left out. With a map extent or an area, it saves each GeoParquet file as a GeoPackage that contains only the features in the area. The GeoPackage takes the file's name with a `.gpkg` extension.

**Download selected** under **All files** saves the files you selected, whole, whatever the area.

A partitioned GeoParquet asset downloads as each file its glob matches, which DuckDB lists first. With a map extent or an area, it downloads as one GeoPackage, named after the folder that contains the partitions.

The bar counts the bytes downloaded when the catalog gives the size of every file, and the files otherwise. It shows the time left.

The plugin saves each file in the folder you pick, at the same relative path as in the catalog, so the copy opens as a local STAC catalog. After each download, the plugin checks the file against its `file:checksum` when the catalog gives one. A second run skips files that are already complete, so it resumes an interrupted download.

## Use another registry or browser

The plugin reads the Portolan registry export at `exports/catalogs.json` and links to the browser at `https://browser.portolan-sdi.org/`. To point the plugin at a mirror, open **Settings > Options > Advanced**. Set `PortolanRegistry/registry_url` for the registry or `PortolanRegistry/browser_url` for the browser, then reopen the window.
