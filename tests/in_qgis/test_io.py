from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from osgeo import gdal, ogr
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QMetaType, QSize, QThread, QVariant, qInstallMessageHandler
from qgis.PyQt.QtGui import QColor, QImage

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.download import (
    WALK_WORKERS,
    Plan,
    PlannedFile,
    Walk,
    local_path,
    plan,
)
from portolan_registry_qgis.core.geoparquet import LAYER
from portolan_registry_qgis.core.mirror import mirror_href
from portolan_registry_qgis.core.scope import Everything
from portolan_registry_qgis.core.stac import Asset, read_document
from portolan_registry_qgis.core.style import DEFAULT_STYLE_NAME, archive_urls
from portolan_registry_qgis.qgis_io import downloader, layers, mosaic, network, parquet_layer
from portolan_registry_qgis.qgis_io.downloader import DownloadJob, target_path
from portolan_registry_qgis.qgis_io.network import (
    NetworkError,
    QtExecutor,
    RemoteRange,
    fetch_bytes,
    fetch_json,
    fetch_range,
    range_fetcher,
    run_task,
)
from portolan_registry_qgis.qgis_io.tileserver import (
    PmtilesError,
    TileServer,
    open_archive,
    read_tile,
)
from tests.loopback import base_url, serve
from tests.tiles import EAST_AREA, TRIANGLE, WEST_VIEW

from .conftest import wait_for


@pytest.fixture
def collection(catalog):
    href = f"{catalog['base']}/points/collection.json"
    return read_document(fetch_json(href), href)


@pytest.fixture
def server():
    tiles = TileServer()
    yield tiles
    tiles.stop()


def test_fetch_json_and_errors(catalog):
    assert fetch_json(catalog["url"])["id"] == "test"
    with pytest.raises(NetworkError):
        fetch_bytes(f"{catalog['base']}/nothing-here.json")
    with pytest.raises(ValueError, match="did not return JSON"):
        fetch_json(f"{catalog['base']}/points.pmtiles")


def test_run_task_reports_on_main_thread(catalog):
    results = []
    run_task("fetch", lambda _task: fetch_json(catalog["url"]), lambda r, e: results.append((r, e)))
    wait_for(lambda: results)
    assert results[0][0]["id"] == "test"
    assert results[0][1] is None

    failures = []
    run_task(
        "fail",
        lambda _task: fetch_bytes(f"{catalog['base']}/x"),
        lambda r, e: failures.append((r, e)),
    )
    wait_for(lambda: failures)
    assert failures[0][0] is None
    assert isinstance(failures[0][1], NetworkError)


def test_run_task_logs_an_error_in_its_callback(catalog, monkeypatch):
    # QGIS drops an exception in a task's finished handler. The failure goes
    # to the message log instead, so it does not pass in silence. QGIS 4
    # emits no messageReceived signal without a GUI, so the test reads the
    # report itself.
    logged = []
    monkeypatch.setattr(network, "report_failure", logged.append)

    def broken(_result, _error):
        raise ValueError("broken on purpose")

    run_task("broken", lambda _task: 1, broken)
    wait_for(lambda: logged)
    assert "broken failed after the task ended" in logged[0]
    assert "ValueError: broken on purpose" in logged[0]


def test_archive_header_and_tiles(catalog):
    archive = open_archive(f"{catalog['base']}/points.pmtiles")
    assert archive.min_zoom == 0
    assert archive.max_zoom == 8
    assert archive.vector_layers == ("points",)
    west, south, _east, _north = archive.bounds
    assert 10.9 < west < 11.1
    assert 43.9 < south < 44.1
    tile = read_tile(archive, 0, 0, 0)
    assert tile
    assert not tile.startswith(b"\x1f\x8b")
    assert read_tile(archive, 8, 0, 0) is None


def test_archive_errors(catalog):
    with pytest.raises(PmtilesError, match="Not a PMTiles"):
        open_archive(f"{catalog['base']}/catalog.json")
    with pytest.raises(NetworkError):
        open_archive(f"{catalog['base']}/gone.pmtiles")


def test_archive_from_a_local_path(catalog):
    archive = open_archive(str(catalog["root"] / "points.pmtiles"))
    assert archive.vector_layers == ("points",)
    assert read_tile(archive, 0, 0, 0)


def test_fetch_range(catalog):
    url = f"{catalog['base']}/catalog.json"
    whole = (catalog["root"] / "catalog.json").read_bytes()
    assert fetch_range(url, 5, 10) == whole[5:15]
    assert range_fetcher(url)(0, 7) == whole[:7]
    local = range_fetcher(str(catalog["root"] / "catalog.json"))
    assert local(5, 10) == whole[5:15]
    as_url = range_fetcher((catalog["root"] / "catalog.json").as_uri())
    assert as_url(5, 10) == whole[5:15]


def test_tile_server_answers(catalog, server):
    archive = open_archive(f"{catalog['base']}/points.pmtiles")
    template = server.register(archive)
    assert server.register(archive) == template
    tile_url = template.replace("{z}/{x}/{y}", "0/0/0")
    with urllib.request.urlopen(tile_url) as response:
        assert response.status == 200
        assert response.read() == read_tile(archive, 0, 0, 0)
    with urllib.request.urlopen(template.replace("{z}/{x}/{y}", "8/0/0")) as response:
        assert response.status == 204
    for bad in ("nope/0/0/0.pbf", template.split("/")[3] + "/a/b/c.pbf", "x"):
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{template.split(':')[2].split('/')[0]}/{bad}")
        assert error.value.code == 404
    assert server.serves(template.replace("{z}", "1"))
    # QGIS 4 percent-encodes the url parameter in layer.source().
    assert server.serves("type=xyz&url=" + urllib.parse.quote(template, safe=""))
    assert not server.serves("http://127.0.0.1:1/x/1/1/1.pbf")
    server.stop()
    assert not server.serves(template)


def test_range_fetcher_reads_any_http_scheme(catalog):
    url = f"{catalog['base']}/catalog.json".replace("http://", "HTTP://", 1)
    assert isinstance(range_fetcher(url), RemoteRange)


def _render(layer, extent):
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    settings.setOutputSize(QSize(256, 256))
    settings.setBackgroundColor(QColor(255, 255, 255))
    settings.setExtent(extent)
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    return job.renderedImage()


def _pixels(image: QImage, channel: str) -> int:
    """Count the sampled pixels that show pure ``channel``: red, green, or blue."""
    count = 0
    for x in range(0, image.width(), 2):
        for y in range(0, image.height(), 2):
            color = image.pixelColor(x, y)
            levels = {"red": color.red(), "green": color.green(), "blue": color.blue()}
            if levels.pop(channel) > 200 and max(levels.values()) < 80:
                count += 1
    return count


# Web Mercator extent around the points near Bologna.
BOLOGNA = QgsRectangle(1_200_000, 5_440_000, 1_500_000, 5_640_000)


def _style_colors(layer) -> set[str]:
    """Return the fill colors of the layer's current renderer, alpha included.

    Stroke colors stay out. The converter and QGIS share a default gray stroke.
    """
    colors = set()
    for style in layer.renderer().styles():
        symbol = style.symbol()
        if symbol is None:
            continue
        for index in range(symbol.symbolLayerCount()):
            color = symbol.symbolLayer(index).color()
            if color.isValid() and color.alpha() > 0:
                colors.add(color.name(QColor.NameFormat.HexArgb))
    return colors


def test_tiles_carry_every_style_as_a_named_style(catalog, collection, server):
    urls = archive_urls(collection)
    assert urls == [f"{catalog['base']}/points.pmtiles"]
    prepared = layers.prepare_pmtiles(collection, urls, fetch_bytes)
    assert prepared.warnings == []
    built, _ = layers.build_pmtiles(server, prepared, QImage.fromData)
    (layer,) = built
    assert layer.isValid()
    assert layer.name() == "Point tiles"
    assert layer.customProperty(layers.PMTILES_PROPERTY).endswith("/points.pmtiles")
    manager = layer.styleManager()
    assert sorted(manager.styles()) == sorted([DEFAULT_STYLE_NAME, "style-red", "style-blue"])
    # The default style is current, and each named style draws its own color.
    assert manager.currentStyle() == "style-red"
    image = _render(layer, BOLOGNA)
    assert _pixels(image, "red") > 0
    assert _pixels(image, "blue") == 0
    manager.setCurrentStyle("style-blue")
    image = _render(layer, BOLOGNA)
    assert _pixels(image, "blue") > 0
    assert _pixels(image, "red") == 0
    # QGIS colors a new vector tile layer from a random palette, which can
    # land on a red. So the default style must share no color with any
    # catalog style, alpha included, rather than draw no red pixels.
    catalog_colors = set()
    for name in ("style-red", "style-blue"):
        manager.setCurrentStyle(name)
        catalog_colors |= _style_colors(layer)
    assert {"#ffff0000", "#ff0000ff"} <= catalog_colors
    manager.setCurrentStyle(DEFAULT_STYLE_NAME)
    default_colors = _style_colors(layer)
    assert default_colors
    assert default_colors.isdisjoint(catalog_colors)
    manager.setCurrentStyle("style-red")
    assert _pixels(_render(layer, BOLOGNA), "red") > 0


def test_tiles_without_styles_use_the_qgis_style(catalog, collection, server):
    bare = replace(collection, assets=collection.data_assets)
    urls = archive_urls(bare)
    prepared = layers.prepare_pmtiles(bare, urls, fetch_bytes)
    assert prepared.warnings == []
    built, _ = layers.build_pmtiles(server, prepared, QImage.fromData)
    (layer,) = built
    assert layer.styleManager().styles() == [DEFAULT_STYLE_NAME]


def test_tiles_report_the_archive_extent(catalog, server):
    """The layer extent is the archive bounds, not the Web Mercator world."""
    archive = open_archive(f"{catalog['base']}/points.pmtiles")
    layer = layers.vector_tile_layer(server, archive, "points")
    world = layer.dataProvider().extent()
    extent = layer.extent()
    assert extent.width() < world.width() / 100
    to_degrees = QgsCoordinateTransform(
        layer.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()
    )
    degrees = to_degrees.transformBoundingBox(extent)
    west, south, east, north = archive.bounds
    assert degrees.xMinimum() == pytest.approx(west, abs=1e-6)
    assert degrees.yMinimum() == pytest.approx(south, abs=1e-6)
    assert degrees.xMaximum() == pytest.approx(east, abs=1e-6)
    assert degrees.yMaximum() == pytest.approx(north, abs=1e-6)
    # A new data source resets the extent to the world. The plugin applies
    # the bounds again when it reopens the layer of a saved project.
    layer.setDataSource(layer.source(), layer.name(), layer.providerType())
    assert layer.extent() == world
    layers.apply_bounds(layer, archive)
    assert layer.extent() == extent
    # An archive without bounds keeps the world extent.
    layers.apply_bounds(layer, replace(archive, bounds=None))
    assert layer.extent() == extent
    bare = layers.vector_tile_layer(server, replace(archive, bounds=None), "points")
    assert bare.extent() == world


def test_unreadable_style_is_skipped_with_a_warning(collection, server):
    red, blue = collection.styles
    broken = replace(red, href=red.href.replace("red", "gone"))
    others = [a for a in collection.assets if a.key != red.key]
    document = replace(collection, assets=(broken, *others))
    urls = archive_urls(document)
    prepared = layers.prepare_pmtiles(document, urls, fetch_bytes)
    assert "Could not read the style" in prepared.warnings[0]
    built, _ = layers.build_pmtiles(server, prepared, QImage.fromData)
    assert built[0].styleManager().currentStyle() == blue.key


def test_styles_follow_the_picked_archive(catalog, collection, server):
    # Both styles read points.pmtiles, so neither one may replace the archive
    # the user picked or show in its style list.
    other = f"{catalog['base']}/other.pmtiles"
    prepared = layers.prepare_pmtiles(collection, [other], fetch_bytes)
    (part,) = prepared.parts
    assert part.archive.url == other
    assert part.title == "Test points"
    assert part.styles == []
    assert prepared.warnings == [f"No style in Test points draws {other}."]


def test_asset_layers(catalog, collection):
    by_key = {asset.key: asset for asset in collection.assets}
    assert layers.asset_layer(by_key["geojson"]).isValid()
    raster = layers.asset_layer(by_key["relief"])
    assert raster.isValid()
    assert raster.providerType() == "gdal"
    for key in ("style-red", "data"):
        with pytest.raises(layers.LayerError, match="no format"):
            layers.asset_layer(by_key[key])


def test_asset_layers_read_only_the_asset(catalog, collection):
    # A plain /vsicurl/ path makes GDAL list the parent directory and probe
    # side-car files. The asset's own URL is the only one GDAL may request.
    # A fresh query string keeps QGIS and GDAL from reusing an earlier open,
    # and checks that the query reaches the server intact.
    by_key = {asset.key: asset for asset in collection.assets}
    for key in ("relief", "geojson"):
        asset = replace(by_key[key], href=f"{by_key[key].href}?probe={key}&x=%2F")
        gdal.VSICurlClearCache()
        catalog["requests"].clear()
        layer = layers.asset_layer(asset, f"{key} alone")
        assert layer.isValid()
        parts = urllib.parse.urlsplit(asset.href)
        assert {path for _method, path in catalog["requests"]} == {f"{parts.path}?{parts.query}"}
        assert layer.source().startswith("/vsicurl?empty_dir=yes&url=")


@pytest.fixture
def tif_only():
    # A QGIS 3.40 session set CPL_VSIL_CURL_ALLOWED_EXTENSIONS to TIF. GDAL
    # then refused every /vsicurl?empty_dir=yes path, so no COG opened.
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", "TIF")
    gdal.VSICurlClearCache()
    yield
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", None)
    gdal.VSICurlClearCache()


@pytest.mark.usefixtures("tif_only")
def test_a_tif_only_setting_still_opens_cogs(catalog, collection, tiles):
    by_key = {asset.key: asset for asset in collection.assets}
    raster = layers.asset_layer(by_key["relief"])
    assert raster.isValid()
    assert raster.source() == f"/vsicurl/{by_key['relief'].href}"
    shares = []
    path = mosaic.write_vrt([_tile(catalog, "a"), _tile(catalog, "b")], progress=shares.append)
    assert path.exists()
    assert shares[-1] == 1.0
    parquet_layer.discard_path(path)


@pytest.mark.usefixtures("tif_only")
def test_a_tif_only_setting_explains_a_blocked_file(catalog, collection):
    by_key = {asset.key: asset for asset in collection.assets}
    with pytest.raises(layers.LayerError) as failure:
        layers.asset_layer(by_key["geojson"])
    message = str(failure.value)
    assert "CPL_VSIL_CURL_ALLOWED_EXTENSIONS is 'TIF'" in message
    assert "so GDAL reads no geojson file over HTTP" in message
    assert "Settings > Options" in message


def _parquet_url(catalog):
    return f"{catalog['base']}/points.parquet"


def _load(url, extent=None, extent_crs=None):
    context = QgsProject.instance().transformContext()
    planned = parquet_layer.plan(url, context, parquet_layer.Limits(extent, extent_crs))
    prepared = parquet_layer.prepare(planned)
    return planned, prepared


def test_planner_plans_in_a_task(catalog):
    results = []
    read = parquet_layer.planner(_parquet_url(catalog), parquet_layer.Limits())
    run_task("plan", read, lambda r, e: results.append((r, e)))
    wait_for(lambda: results)
    planned, error = results[0]
    assert error is None
    assert planned.estimate == 200


def test_is_interrupt():
    duckdb = pytest.importorskip("duckdb")
    assert parquet_layer.is_interrupt(duckdb.InterruptException("stop"))
    assert not parquet_layer.is_interrupt(ValueError("other"))
    assert not parquet_layer.is_interrupt(None)


def test_start_vrt_reports_the_path_or_the_error(catalog):
    results = []
    mosaic.start_vrt("vrt", [_tile(catalog, "a")], None, lambda p, e: results.append((p, e)))
    mosaic.start_vrt("none", [], None, lambda p, e: results.append((p, e)))
    wait_for(lambda: len(results) == 2)
    by_error = {error is None: (path, error) for path, error in results}
    path, _ = by_error[True]
    assert path is not None
    assert path.exists()
    parquet_layer.discard_path(path)
    path, error = by_error[False]
    assert path is None
    assert isinstance(error, layers.LayerError)


def test_parquet_layer_whole_file(catalog):
    planned, prepared = _load(_parquet_url(catalog))
    assert planned.estimate == 200
    assert prepared.written.features == 200
    assert prepared.path.parent.parent == parquet_layer.scratch_folder()
    layer, refused = parquet_layer.build(prepared, "points")
    assert refused == 0
    assert layer.isValid()
    assert layer.providerType() == "ogr"
    assert layer.featureCount() == 200
    assert QgsWkbTypes.displayString(layer.wkbType()) == "Point"
    assert layer.crs().isGeographic()
    names = [f.name() for f in layer.fields()]
    assert names == ["fid", "id", "name", "score", "day", "seen"]
    first = next(layer.getFeatures())
    assert first["name"] == "p0"
    assert first.geometry().asPoint().x() == pytest.approx(11.0)
    assert layer.customProperty(parquet_layer.SOURCE_PROPERTY) == _parquet_url(catalog)


def test_parquet_dates_load(catalog):
    """Regression: a DATE or TIMESTAMP column used to load no features at all."""
    _, prepared = _load(_parquet_url(catalog))
    layer, _ = parquet_layer.build(prepared, "points")
    fields = layer.fields()
    assert fields.field("day").type() in (QVariant.Date, QMetaType.Type.QDate)
    assert fields.field("seen").type() in (QVariant.DateTime, QMetaType.Type.QDateTime)
    feature = next(layer.getFeatures())
    assert feature["day"] == QDate(2026, 1, 1)
    # The column holds a TIMESTAMP without a zone, so the wall-clock time survives.
    assert feature["seen"].toString("yyyy-MM-dd HH:mm") == "2026-01-01 03:00"


def test_parquet_layer_extent(catalog):
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    to_mercator = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        mercator,
        QgsProject.instance().transformContext(),
    )
    extent = to_mercator.transformBoundingBox(QgsRectangle(11.095, 44.0, 11.305, 45.0))
    planned, prepared = _load(_parquet_url(catalog), extent, mercator)
    assert planned.estimate == 21
    layer, _ = parquet_layer.build(prepared, "points")
    assert sorted(f["id"] for f in layer.getFeatures()) == list(range(10, 31))


def test_parquet_layer_needs_duckdb(catalog, monkeypatch):
    monkeypatch.setattr(parquet_query, "duckdb_status", lambda: (False, None))
    monkeypatch.setattr(parquet_query, "_connection", None)
    with pytest.raises(parquet_query.DuckDBMissingError):
        parquet_layer.plan(_parquet_url(catalog), QgsProject.instance().transformContext())


def test_parquet_crs_fallback():
    from portolan_registry_qgis.core.geoparquet import plan_read

    plan = plan_read([("g", "GEOMETRY")], '{"columns": {"g": {"crs": "not a crs"}}}')
    assert parquet_layer.crs_for(plan).authid() == "EPSG:4326"
    plan = plan_read(
        [("g", "GEOMETRY")],
        '{"columns": {"g": {"crs": {"id": {"authority": "EPSG", "code": 2272}}}}}',
    )
    assert parquet_layer.crs_for(plan).authid() == "EPSG:2272"


def test_sweep_keeps_files_that_layers_read(catalog):
    project = QgsProject.instance()
    _, kept = _load(_parquet_url(catalog))
    _, shared = _load(_parquet_url(catalog))
    layer, _ = parquet_layer.build(kept, "kept")
    first, _ = parquet_layer.build(shared, "first")
    project.addMapLayers([layer, first])
    # A duplicated layer reads the same file as the original.
    second = first.clone()
    project.addMapLayer(second)
    _, unused = _load(_parquet_url(catalog))
    pending = unused.path
    parquet_layer.sweep(project)
    # A copy that is not a layer yet is still being opened. The sweep keeps it.
    assert pending.exists()
    parquet_layer.discard_path(pending)
    assert not pending.exists()
    project.removeMapLayer(first.id())
    parquet_layer.sweep(project)
    assert shared.path.exists()
    project.removeMapLayer(second.id())
    parquet_layer.sweep(project)
    assert not shared.path.exists()
    assert kept.path.exists()
    project.removeAllMapLayers()
    parquet_layer.sweep(project)
    assert not kept.path.exists()


def test_sweep_covers_earlier_plugin_loads(catalog, monkeypatch):
    """A plugin reload forgets its session folder. The sweep still finds the files."""
    project = QgsProject.instance()
    _, before = _load(_parquet_url(catalog))
    project.addMapLayer(parquet_layer.build(before, "before")[0])
    monkeypatch.setattr(parquet_layer, "_session", None)
    _, after = _load(_parquet_url(catalog))
    project.addMapLayer(parquet_layer.build(after, "after")[0])
    assert before.path.parent != after.path.parent
    project.removeAllMapLayers()
    parquet_layer.sweep(project)
    assert not before.path.exists()
    assert not after.path.exists()


def test_remove_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(parquet_layer, "scratch_folder", lambda **_: tmp_path)
    old = tmp_path / "session-1-old"
    old.mkdir()
    (old / "a.gpkg").write_bytes(b"x")
    # Another QGIS deleted this file during the scan. The scan skips it.
    (old / "gone.gpkg").symlink_to(tmp_path / "missing")
    recent = tmp_path / "session-1-recent"
    recent.mkdir()
    (recent / "b.gpkg").write_bytes(b"x")
    own = tmp_path / f"session-{os.getpid()}-own"
    own.mkdir()
    (own / "c.gpkg").write_bytes(b"x")
    other = tmp_path / "duckdb"
    other.mkdir()
    week_ago = (recent / "b.gpkg").stat().st_mtime - parquet_layer.STALE_SECONDS - 60
    for path in (old / "a.gpkg", own / "c.gpkg"):
        os.utime(path, (week_ago, week_ago))
    parquet_layer.remove_stale()
    assert not old.exists()
    assert recent.exists()
    # This QGIS's folder stays, however old its files are.
    assert own.exists()
    assert other.exists()


def test_remove_stale_creates_nothing(tmp_path, monkeypatch):
    missing = tmp_path / "layers"
    monkeypatch.setattr(parquet_layer, "scratch_folder", lambda **_: missing)
    parquet_layer.remove_stale()
    assert not missing.exists()


def test_needs_restore_finds_a_missing_copy(tmp_path):
    # On QGIS 3.34 the layer keeps the path of the missing copy.
    gone = tmp_path / "portolan_registry" / "layers" / "session-1-x" / ("0" * 32 + ".gpkg")
    layer = QgsVectorLayer(f"{gone}|layername=features", "gone", "ogr")
    assert not layer.isValid()
    assert not parquet_layer.needs_restore(layer)
    layer.setCustomProperty(parquet_layer.SOURCE_PROPERTY, "https://x.test/a.parquet")
    assert parquet_layer.needs_restore(layer)
    # A missing file outside the scratch folder is the user's own layer.
    other = QgsVectorLayer(str(tmp_path / "roads.gpkg"), "roads", "ogr")
    other.setCustomProperty(parquet_layer.SOURCE_PROPERTY, "https://x.test/a.parquet")
    assert not parquet_layer.needs_restore(other)


def test_restore_path(tmp_path, monkeypatch):
    monkeypatch.setattr(parquet_layer, "scratch_folder", lambda **_: tmp_path)
    assert parquet_layer.restore_path("/data/roads.gpkg") == "/data/roads.gpkg"
    session = tmp_path / "session-1-abc"
    session.mkdir()
    name = "0123456789abcdef0123456789abcdef.gpkg"
    (session / name).write_bytes(b"x")
    # A project saved with relative paths keeps a path relative to the project file.
    saved = f"../../profile/portolan_registry/layers/session-1-abc/{name}"
    assert parquet_layer.restore_path(saved) == str(session / name)
    gone = f"/home/u/portolan_registry/layers/session-2-def/{name}|layername=features"
    restored = parquet_layer.restore_path(gone)
    assert restored == f"{tmp_path / 'placeholder.gpkg'}|layername=features"
    placeholder = QgsVectorLayer(restored, "placeholder", "ogr")
    assert placeholder.isValid()
    assert placeholder.featureCount() == 0


def _download(folder, files):
    reports = []
    job = DownloadJob(folder, files)
    job.finished.connect(reports.append)
    job.start()
    wait_for(lambda: reports, timeout=60)
    return reports[0]


def test_download_verifies_and_resumes(catalog, collection, tmp_path):
    files = [
        PlannedFile(a.href, local_path(a.href, catalog["url"]), a.size, a.checksum)
        for a in collection.assets
    ]
    first = _download(tmp_path, files)
    assert first.failed == []
    assert len(first.downloaded) == len(files)
    assert first.verified == len(files)
    assert (tmp_path / "points" / "relief.tif").read_bytes() == (
        catalog["root"] / "points" / "relief.tif"
    ).read_bytes()
    second = _download(tmp_path, files)
    assert second.downloaded == []
    assert len(second.skipped) == len(files)


def test_download_rejects_bad_checksum_and_missing_files(catalog, tmp_path):
    url = f"{catalog['base']}/catalog.json"
    files = [
        PlannedFile(url, "catalog.json", None, "1220" + "00" * 32),
        PlannedFile(f"{catalog['base']}/missing.json", "missing.json"),
    ]
    report = _download(tmp_path, files)
    failed = dict(report.failed)
    assert sorted(failed) == ["catalog.json", "missing.json"]
    assert "do not match" in failed["catalog.json"]
    assert not (tmp_path / "catalog.json").exists()


def test_target_path_stays_inside(tmp_path):
    assert target_path(tmp_path, PlannedFile("u", "a/b.txt")) == tmp_path / "a" / "b.txt"
    with pytest.raises(ValueError, match="outside"):
        target_path(tmp_path, PlannedFile("u", "../escape.txt"))


def test_cancel_stops_the_job(catalog, tmp_path):
    files = [PlannedFile(f"{catalog['base']}/points.pmtiles", f"f{i}.pmtiles") for i in range(5)]
    reports = []
    job = DownloadJob(tmp_path, files)
    job.finished.connect(reports.append)
    job.start()
    job.cancel()
    wait_for(lambda: reports)
    assert reports[0].cancelled
    assert len(reports) == 1
    QgsProject.instance().clear()


def test_download_runs_files_at_the_same_time(tmp_path):
    # The server answers no file until three requests wait at the same time,
    # so a job that downloads one file after another times out.
    barrier = threading.Barrier(3, timeout=10)
    lock = threading.Lock()
    active = peak = 0

    class Gate(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                self.send_error(504)
                return
            finally:
                with lock:
                    active -= 1
            body = self.path.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with serve(Gate) as gate:
        base = base_url(gate)
        files = [PlannedFile(f"{base}/f{i}", f"f{i}.txt") for i in range(6)]
        report = _download(tmp_path, files)
    assert report.failed == []
    assert sorted(report.downloaded) == [f"f{i}.txt" for i in range(6)]
    assert peak == 3
    assert (tmp_path / "f4.txt").read_text() == "/f4"


def test_download_hashes_while_the_next_file_downloads(catalog, collection, tmp_path, monkeypatch):
    # The hash of the first file waits until the server sees the request for
    # the second file. A job that hashes before the next download times out.
    relief = next(a for a in collection.assets if a.key == "relief")
    # The query keeps the QGIS network cache from answering an earlier test's copy.
    second = f"{catalog['base']}/points.pmtiles?overlap"
    real = downloader.file_matches
    overlapped = []

    def slow_hash(path, checksum, cancelled=None):
        deadline = time.monotonic() + 10
        while ("GET", "/points.pmtiles?overlap") not in catalog["requests"]:
            if time.monotonic() > deadline:
                overlapped.append(False)
                return real(path, checksum, cancelled=cancelled)
            time.sleep(0.01)
        overlapped.append(True)
        return real(path, checksum, cancelled=cancelled)

    monkeypatch.setattr(downloader, "file_matches", slow_hash)
    catalog["requests"].clear()
    files = [
        PlannedFile(relief.href, "relief.tif", relief.size, relief.checksum),
        PlannedFile(second, "points.pmtiles"),
    ]
    progress = []
    reports = []
    job = DownloadJob(tmp_path, files)
    job.progress.connect(lambda done, total, _message: progress.append((done, total)))
    job.finished.connect(reports.append)
    job.start()
    wait_for(lambda: reports, timeout=30)
    (report,) = reports
    assert overlapped == [True]
    assert report.failed == []
    assert report.verified == 1
    assert progress[-1] == (2, 2)


def test_cancel_frees_every_slot_once(catalog, tmp_path):
    files = [PlannedFile(f"{catalog['base']}/points.pmtiles", f"f{i}.pmtiles") for i in range(9)]
    reports = []
    job = DownloadJob(tmp_path, files)
    job.finished.connect(reports.append)
    job.start()
    wait_for(lambda: job.downloading)
    job.cancel()
    wait_for(lambda: reports)
    # Let any late downloadError signals arrive. None may report again.
    wait_for(lambda: not job.downloading)
    for _ in range(20):
        QgsApplication.processEvents()
    assert len(reports) == 1
    assert reports[0].cancelled
    assert job.busy == 0
    assert len(reports[0].downloaded) < len(files)


def test_plan_reads_the_catalog_from_a_task(catalog):
    # The window plans inside a QgsTask, and the walk reads from its own
    # threads. Qt timers, which QGIS network requests use, only work on a
    # QThread. A plain Python thread makes Qt print a warning per thread.
    messages = []
    previous = qInstallMessageHandler(lambda _kind, _context, text: messages.append(text))
    try:
        results = []
        run_task(
            "plan",
            lambda task: plan(
                fetch_json,
                catalog["url"],
                how=Walk(cancelled=task.isCanceled, executor=QtExecutor(WALK_WORKERS)),
            ),
            lambda result, error: results.append((result, error)),
        )
        wait_for(lambda: results)
    finally:
        qInstallMessageHandler(previous)
    assert [m for m in messages if "QThread" in m] == []
    result, error = results[0]
    assert error is None
    assert isinstance(result, Plan)
    paths = [f.path for f in result.files]
    assert paths[:2] == ["catalog.json", "points/collection.json"]
    assert "points/relief.tif" in paths
    assert [url for url, _ in result.failures] == [f"{catalog['base']}/missing/collection.json"]


def test_qt_executor_runs_on_qthreads_and_reports_errors():
    executor = QtExecutor(4)
    try:
        assert list(executor.map(lambda n: n * 2, range(10))) == list(range(0, 20, 2))
        main = QThread.currentThread()
        threads = list(executor.map(lambda _n: QThread.currentThread(), range(8)))
        assert all(thread is not main for thread in threads)
        future = executor.submit(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            future.result(timeout=10)
    finally:
        executor.shutdown()


def test_qt_executor_cancels_queued_work():
    executor = QtExecutor(1)
    started = threading.Event()
    release = threading.Event()

    def hold():
        started.set()
        return release.wait(10)

    running = executor.submit(hold)
    assert started.wait(10)
    queued = [executor.submit(lambda: "ran") for _ in range(3)]
    executor.shutdown(wait=False, cancel_futures=True)
    release.set()
    executor.shutdown()
    assert running.result() is True
    assert all(future.cancelled() for future in queued)


def test_resume_of_many_present_files_does_not_recurse(tmp_path):
    # Each present file settles at once. A job that starts the next file
    # from inside the last one recurses once per file and overflows.
    count = sys.getrecursionlimit() + 200
    files = []
    for i in range(count):
        (tmp_path / f"f{i}.txt").write_bytes(b"x")
        files.append(PlannedFile(f"http://127.0.0.1:9/f{i}.txt", f"f{i}.txt", 1))
    report = _download(tmp_path, files)
    assert report.failed == []
    assert len(report.skipped) == count


def test_download_progress_counts_bytes_when_every_size_is_known(catalog, collection, tmp_path):
    files = [
        PlannedFile(a.href, local_path(a.href, catalog["url"]), a.size, a.checksum)
        for a in collection.assets
    ]
    shares = []
    reports = []
    job = DownloadJob(tmp_path, files)
    assert job.fraction == 0.0
    job.progress.connect(lambda *_args: shares.append(job.fraction))
    job.finished.connect(reports.append)
    job.start()
    wait_for(lambda: reports, timeout=30)
    assert shares == sorted(shares)
    assert shares[-1] == 1.0
    # One file with no size makes the share count files.
    unsized = DownloadJob(tmp_path, [*files[:1], PlannedFile(files[1].url, "unsized")])
    assert unsized.fraction == 0.0
    assert DownloadJob(tmp_path, []).fraction is None


def test_files_that_share_a_local_path_fail_before_any_download(catalog, tmp_path):
    first = PlannedFile(f"{catalog['base']}/points.pmtiles?a", "points.pmtiles")
    same = PlannedFile(f"{catalog['base']}/other.pmtiles", "points.pmtiles")
    case = PlannedFile(f"{catalog['base']}/catalog.json", "POINTS.pmtiles")
    report = _download(tmp_path, [first, same, case])
    assert report.downloaded == ["points.pmtiles"]
    assert report.failed == [
        ("points.pmtiles", f"{same.url} has the same local path as another file"),
        ("POINTS.pmtiles", f"{case.url} has the same local path as another file"),
    ]
    assert (tmp_path / "points.pmtiles").read_bytes() == (
        catalog["root"] / "points.pmtiles"
    ).read_bytes()


def test_progress_names_every_busy_slot(catalog, collection, tmp_path, monkeypatch):
    # A slow hash keeps "Verifying relief.tif" on screen while the other
    # slots start their files.
    relief = next(a for a in collection.assets if a.key == "relief")
    release = threading.Event()
    real = downloader.file_matches

    def slow_hash(path, checksum, cancelled=None):
        release.wait(10)
        return real(path, checksum, cancelled=cancelled)

    monkeypatch.setattr(downloader, "file_matches", slow_hash)
    files = [
        PlannedFile(relief.href, "relief.tif", relief.size, relief.checksum),
        PlannedFile(f"{catalog['base']}/catalog.json?progress", "catalog.json"),
    ]
    messages = []
    reports = []
    job = DownloadJob(tmp_path, files)
    job.progress.connect(lambda _done, _total, message: messages.append(message))
    job.finished.connect(reports.append)
    job.start()
    wait_for(lambda: any("Verifying relief.tif" in m for m in messages))
    wait_for(lambda: "catalog.json" in job.report.downloaded)
    assert "Verifying relief.tif" in messages[-1]
    release.set()
    wait_for(lambda: reports)
    assert reports[0].failed == []


# ---------- CSV ----------


def _csv_asset(catalog, name):
    href = f"{catalog['base']}/table/{name}"
    return Asset(name, href, None, "text/csv", ("data",), None, None, "csv")


def test_csv_with_coordinates_opens_as_points(catalog):
    gdal.VSICurlClearCache()
    layer = layers.asset_layer(_csv_asset(catalog, "points.csv"))
    assert layer.isValid()
    assert layer.providerType() == "ogr"
    assert QgsWkbTypes.displayString(layer.wkbType()) == "Point"
    assert layer.crs().authid() == "EPSG:4326"
    assert layer.featureCount() == 20
    assert [f.name() for f in layer.fields()] == ["id", "Name", "Longitude", "LAT"]
    first = next(layer.getFeatures())
    assert first["Name"] == "p0"
    assert first.geometry().asPoint().x() == pytest.approx(11.0)
    assert first.geometry().asPoint().y() == pytest.approx(44.0)


def test_csv_without_coordinates_opens_as_a_table(catalog):
    gdal.VSICurlClearCache()
    layer = layers.asset_layer(_csv_asset(catalog, "plain.csv"), "plain")
    assert layer.isValid()
    assert layer.wkbType() == QgsWkbTypes.Type.NoGeometry
    assert layer.featureCount() == 3
    assert [f.name() for f in layer.fields()] == ["id", "name", "score"]
    assert [f["score"] for f in layer.getFeatures()] == [1.5, 2.5, 3.5]


def test_csv_reads_only_the_header_range(catalog):
    url = f"{catalog['base']}/table/points.csv"
    whole = (catalog["root"] / "table" / "points.csv").read_bytes()
    assert layers.csv_header(url) == whole[: layers.CSV_HEADER_BYTES]
    assert layers.csv_header(f"{catalog['base']}/table/gone.csv") == b""


def test_csv_with_no_header_opens_as_a_table(catalog):
    # A server that ignores range requests gives no header to read.
    url = f"{catalog['base']}/table/points.csv?noheader"
    layer = layers.csv_layer(url, "points", header=lambda _url: b"")
    assert layer.isValid()
    assert layer.wkbType() == QgsWkbTypes.Type.NoGeometry
    assert layer.featureCount() == 20


# ---------- GeoParquet areas and tables ----------

STRIP = "POLYGON((11.095 44.0, 11.305 44.0, 11.305 45.0, 11.095 45.0, 11.095 44.0))"


@pytest.fixture
def area_calls(monkeypatch):
    """Record the clip that reaches parquet_query."""
    calls = []
    estimate, write_gpkg = parquet_query.estimate, parquet_query.write_gpkg

    def call(function, name, args, kwargs):
        # estimate takes the clip fourth, after the plan. write_gpkg takes
        # the target there, which holds the clip.
        clip = args[3] if name == "estimate" else args[3].clip
        calls.append((name, clip))
        return function(*args, **kwargs)

    monkeypatch.setattr(parquet_query, "estimate", lambda *a, **k: call(estimate, "estimate", a, k))
    monkeypatch.setattr(
        parquet_query, "write_gpkg", lambda *a, **k: call(write_gpkg, "write_gpkg", a, k)
    )
    return calls


def test_parquet_layer_area(catalog, area_calls):
    context = QgsProject.instance().transformContext()
    # The area replaces the extent.
    elsewhere = QgsRectangle(0, 0, 1, 1)
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    limits = parquet_layer.Limits(elsewhere, wgs84, area=STRIP)
    planned = parquet_layer.plan(_parquet_url(catalog), context, limits)
    assert planned.clip.bbox == pytest.approx((11.095, 44.0, 11.305, 45.0))
    assert QgsGeometry.fromWkt(planned.clip.area).equals(QgsGeometry.fromWkt(STRIP))
    assert planned.estimate == 21
    prepared = parquet_layer.prepare(planned)
    assert prepared.clip == planned.clip
    assert area_calls == [("estimate", planned.clip), ("write_gpkg", planned.clip)]
    layer, _ = parquet_layer.build(prepared, "strip")
    assert sorted(f["id"] for f in layer.getFeatures()) == list(range(10, 31))
    assert layer.customProperty(parquet_layer.AREA_PROPERTY) == planned.clip.area

    # A saved project copies the same area again.
    area_calls.clear()
    url, bbox, area = parquet_layer.saved_copy(layer)
    assert area == planned.clip.area
    restored = parquet_layer.restore(url, bbox, area_text=area)
    assert restored.clip == planned.clip
    assert area_calls == [("write_gpkg", planned.clip)]
    parquet_layer.discard_path(prepared.path)
    parquet_layer.discard_path(restored.path)


def test_parquet_layer_without_area_keeps_its_property_empty(catalog, area_calls):
    planned, prepared = _load(_parquet_url(catalog))
    assert planned.clip.area is None
    assert all(clip.area is None for _, clip in area_calls)
    layer, _ = parquet_layer.build(prepared, "points")
    assert layer.customProperty(parquet_layer.AREA_PROPERTY) == ""
    assert parquet_layer.saved_copy(layer)[2] == ""


def test_file_area_transforms_to_the_file_crs():
    context = QgsProject.instance().transformContext()
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    clip = parquet_layer.file_area(STRIP, mercator, context)
    wkt, bbox = clip.area, clip.bbox
    expected = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"), mercator, context
    ).transformBoundingBox(QgsRectangle(11.095, 44.0, 11.305, 45.0))
    assert bbox == pytest.approx(
        (expected.xMinimum(), expected.yMinimum(), expected.xMaximum(), expected.yMaximum())
    )
    assert QgsGeometry.fromWkt(wkt).boundingBox().xMinimum() == pytest.approx(bbox[0])
    with pytest.raises(ValueError, match="Not a Polygon"):
        parquet_layer.file_area("LINESTRING(0 0, 1 1)", mercator, context)
    with pytest.raises(ValueError, match="Not a Polygon"):
        parquet_layer.file_area("not wkt", mercator, context)


def _attribute_table(path, rows):
    """Write a GeoPackage that holds one attribute table and no geometry."""
    source = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    table = source.CreateLayer(LAYER, geom_type=ogr.wkbNone)
    table.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    table.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    for i in range(rows):
        feature = ogr.Feature(table.GetLayerDefn())
        feature.SetField("id", i)
        feature.SetField("name", f"r{i}")
        table.CreateFeature(feature)
    source = None


def test_build_opens_a_table_without_geometry():
    path = parquet_layer.new_path(".gpkg")
    _attribute_table(path, 30)
    prepared = parquet_layer.Prepared(
        "https://x.test/plain.parquet", path, parquet_query.Written(30, "None", 0)
    )
    layer, refused = parquet_layer.build(prepared, "plain")
    assert refused == 0
    assert layer.isValid()
    assert layer.wkbType() == QgsWkbTypes.Type.NoGeometry
    assert layer.featureCount() == 30
    assert [f.name() for f in layer.fields()] == ["fid", "id", "name"]
    project = QgsProject.instance()
    project.addMapLayer(layer)
    parquet_layer.sweep(project)
    assert path.exists()
    project.removeMapLayer(layer.id())
    parquet_layer.sweep(project)
    assert not path.exists()


def test_parquet_without_geometry_loads_as_a_table(catalog):
    url = f"{catalog['base']}/table/plain.parquet"
    planned = parquet_layer.plan(url, QgsProject.instance().transformContext())
    assert not planned.plan.has_geometry
    prepared = parquet_layer.prepare(planned)
    layer, _ = parquet_layer.build(prepared, "plain")
    assert layer.isValid()
    assert layer.wkbType() == QgsWkbTypes.Type.NoGeometry
    assert layer.featureCount() == 30
    assert sorted(f["id"] for f in layer.getFeatures()) == list(range(30))


def test_new_path_refuses_other_files():
    with pytest.raises(ValueError, match=r"no \.txt files"):
        parquet_layer.new_path(".txt")


# ---------- COG mosaics ----------


@pytest.fixture
def tiles(catalog):
    href = f"{catalog['base']}/tiles/collection.json"
    return read_document(fetch_json(href), href)


def _tile(catalog, key):
    return f"{catalog['base']}/tiles/{key}/{key}.tif"


def test_item_hrefs_from_the_mirror(catalog, tiles, monkeypatch):
    def no_walk(*_args, **_kwargs):
        raise AssertionError("The mirror must answer without a walk")

    monkeypatch.setattr(mosaic, "walk", no_walk)
    assert mirror_href(tiles) == f"{catalog['base']}/tiles/items.parquet"
    both = [_tile(catalog, "a"), _tile(catalog, "b")]
    assert mosaic.item_hrefs_in(tiles, Everything(), fetch_json) == both
    assert mosaic.item_hrefs_in(tiles, WEST_VIEW, fetch_json) == both[:1]
    assert mosaic.item_hrefs_in(tiles, EAST_AREA, fetch_json) == both[1:]
    # DuckDB tests the item geometry against the polygon, not its envelope.
    assert mosaic.item_hrefs_in(tiles, TRIANGLE, fetch_json) == both[:1]


def test_item_hrefs_by_walk(catalog, tiles, monkeypatch):
    monkeypatch.setattr(parquet_query, "duckdb_status", lambda: (False, None))
    both = [_tile(catalog, "a"), _tile(catalog, "b")]
    assert mosaic.item_hrefs_in(tiles, Everything(), fetch_json) == both
    executor = QtExecutor(WALK_WORKERS)
    try:
        assert mosaic.item_hrefs_in(tiles, WEST_VIEW, fetch_json, executor) == both[:1]
        assert mosaic.item_hrefs_in(tiles, EAST_AREA, fetch_json, executor) == both[1:]
    finally:
        executor.shutdown()
    assert mosaic.item_hrefs_in(tiles, Everything(), fetch_json, cancelled=lambda: True) == []


def test_item_hrefs_fall_back_to_the_walk(catalog, tiles):
    (mirror,) = tiles.assets
    broken = replace(tiles, assets=(replace(mirror, href=f"{catalog['base']}/tiles/gone.parquet"),))
    assert mosaic.item_hrefs_in(broken, WEST_VIEW, fetch_json) == [_tile(catalog, "a")]


def _sample(layer, x, y):
    value, ok = layer.dataProvider().sample(QgsPointXY(x, y), 1)
    return value if ok else None


def test_build_mosaic(catalog):
    hrefs = [_tile(catalog, "a"), _tile(catalog, "b")]
    layer = mosaic.build_mosaic(hrefs, "tiles")
    assert layer.isValid()
    assert layer.providerType() == "gdal"
    assert layer.name() == "tiles"
    assert layer.bandCount() == 1
    extent = layer.extent()
    assert extent.xMinimum() == pytest.approx(11.0)
    assert extent.xMaximum() == pytest.approx(12.64)
    assert _sample(layer, 11.3, 44.3) == 50
    assert _sample(layer, 12.3, 44.3) == 150
    assert mosaic.sources(layer) == hrefs
    path = Path(layer.source())
    assert path.suffix == ".vrt"
    assert path.parent.parent == parquet_layer.scratch_folder()
    assert not mosaic.needs_rebuild(layer)

    project = QgsProject.instance()
    project.addMapLayer(layer)
    parquet_layer.sweep(project)
    assert path.exists()
    project.removeMapLayer(layer.id())
    parquet_layer.sweep(project)
    assert not path.exists()


def test_build_mosaic_errors(catalog):
    with pytest.raises(layers.LayerError, match="No file"):
        mosaic.build_mosaic([], "nothing")
    with pytest.raises(layers.LayerError, match="could not build"):
        mosaic.build_mosaic([f"{catalog['base']}/tiles/gone.tif"], "gone")
    with pytest.raises(mosaic.MosaicCancelledError):
        mosaic.write_vrt([_tile(catalog, "a")], cancelled=lambda: True)
    session = parquet_layer.new_path(".vrt").parent
    assert list(session.glob("*.vrt")) == []


def test_a_mosaic_says_why_gdal_left_a_file_out(catalog, monkeypatch):
    # BuildVRT reports only "Can't open <path>. Skipping it." The mosaic asks
    # GDAL for the reason, such as the HTTP status.
    logged = []
    monkeypatch.setattr(mosaic, "log", logged.append)
    gone = f"{catalog['base']}/tiles/gone-{id(logged)}.tif"
    with pytest.raises(layers.LayerError) as failure:
        mosaic.write_vrt([gone])
    assert f"GDAL could not open {gone}" in str(failure.value)
    assert "404" in str(failure.value)
    # With one good file, the mosaic holds it and the log names the other.
    path = mosaic.write_vrt([_tile(catalog, "a"), gone])
    assert path.exists()
    assert any(
        entry.startswith(f"The mosaic left out {gone}:") and "404" in entry for entry in logged
    )
    parquet_layer.discard_path(path)


def test_a_failed_read_does_not_stay_failed(catalog):
    # GDAL remembers a failed request for the life of the process. A file
    # that a busy server refused once must open when it answers again.
    name = f"later-{id(catalog)}.tif"
    href = f"{catalog['base']}/tiles/{name}"
    with pytest.raises(layers.LayerError):
        mosaic.write_vrt([href])
    target = catalog["root"] / "tiles" / name
    shutil.copy(catalog["root"] / "tiles" / "a" / "a.tif", target)
    try:
        path = mosaic.write_vrt([href])
        assert path.exists()
        parquet_layer.discard_path(path)
        cog = Asset(name, href, None, "image/tiff", ("data",), None, None, "cog")
        assert layers.asset_layer(cog).isValid()
    finally:
        target.unlink()


def _alpha(layer, x, y):
    """Return the alpha of the pixel at ``(x, y)``: 255 inside the clip, 0 outside."""
    value, ok = layer.dataProvider().sample(QgsPointXY(x, y), layer.bandCount())
    return value if ok else None


def test_a_clipped_mosaic_shows_only_the_area(catalog):
    # Users expect "data inside the polygon", not every file the polygon
    # touches. TRIANGLE touches both files. The mosaic shows only its pixels.
    hrefs = [_tile(catalog, "a"), _tile(catalog, "b")]
    layer = mosaic.build_mosaic(hrefs, "clipped", TRIANGLE.wkt)
    assert layer.isValid()
    assert mosaic.clip_of(layer) == TRIANGLE.wkt
    # The file holds the mosaic and the cutline. No other scratch file.
    path = Path(layer.source())
    assert "<Cutline>" in path.read_text() or "Cutline" in path.read_text()
    # The extent is the triangle's envelope, not the extent of the two files.
    # North of the files, the envelope holds transparent pixels.
    extent = layer.extent()
    assert (extent.xMinimum(), extent.xMaximum()) == (
        pytest.approx(11.5, abs=0.01),
        pytest.approx(12.3, abs=0.01),
    )
    assert (extent.yMinimum(), extent.yMaximum()) == (
        pytest.approx(44.3, abs=0.01),
        pytest.approx(44.9, abs=0.01),
    )
    # The files have no nodata value, so the clip adds an alpha band.
    assert layer.bandCount() == 2
    assert _sample(layer, 11.55, 44.6) == 50
    assert _alpha(layer, 11.55, 44.6) == 255
    # Inside the envelope but outside the triangle.
    assert _alpha(layer, 12.25, 44.35) == 0
    parquet_layer.discard_path(path)


def test_a_saved_clipped_mosaic_rebuilds_clipped(catalog):
    hrefs = [_tile(catalog, "a"), _tile(catalog, "b")]
    layer = mosaic.build_mosaic(hrefs, "clipped", TRIANGLE.wkt)
    gone = Path(layer.source())
    parquet_layer.discard_path(gone)
    mosaic.rebuild(layer)
    assert layer.isValid()
    assert Path(layer.source()) != gone
    assert layer.bandCount() == 2
    assert _alpha(layer, 12.25, 44.35) == 0
    assert layer.extent().xMaximum() == pytest.approx(12.3, abs=0.01)


def test_rebuild_a_saved_mosaic(catalog):
    hrefs = [_tile(catalog, "a"), _tile(catalog, "b")]
    layer = mosaic.build_mosaic(hrefs, "tiles")
    gone = Path(layer.source())
    parquet_layer.discard_path(gone)
    # A saved project reads the placeholder in place of the lost VRT.
    stand_in = parquet_layer.restore_path(str(gone))
    assert Path(stand_in).name == "placeholder.vrt"
    layer.setDataSource(stand_in, layer.name(), "gdal")
    assert layer.isValid()
    assert mosaic.needs_rebuild(layer)
    mosaic.rebuild(layer)
    assert layer.isValid()
    assert not mosaic.needs_rebuild(layer)
    assert Path(layer.source()) != gone
    assert _sample(layer, 12.3, 44.3) == 150
    assert mosaic.sources(layer) == hrefs
    parquet_layer.discard_path(Path(layer.source()))


def test_needs_rebuild_finds_a_missing_vrt(tmp_path):
    # On QGIS 3.34 the layer keeps the path of the missing VRT.
    gone = tmp_path / "portolan_registry" / "layers" / "session-1-x" / ("0" * 32 + ".vrt")
    layer = QgsRasterLayer(str(gone), "gone", "gdal")
    assert not mosaic.needs_rebuild(layer)
    layer.setCustomProperty(mosaic.SOURCES_PROPERTY, "https://x.test/a.tif")
    assert mosaic.needs_rebuild(layer)
    with pytest.raises(layers.LayerError, match="keeps no mosaic sources"):
        mosaic.rebuild(QgsRasterLayer(str(gone), "bare", "gdal"))
