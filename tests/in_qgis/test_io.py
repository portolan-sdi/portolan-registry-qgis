from __future__ import annotations

import os
import urllib.parse
import urllib.request
from dataclasses import replace

import pytest
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QMetaType, QSize, QVariant
from qgis.PyQt.QtGui import QColor, QImage

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.download import PlannedFile, local_path
from portolan_registry_qgis.core.stac import read_document
from portolan_registry_qgis.qgis_io import layers, parquet_layer
from portolan_registry_qgis.qgis_io.downloader import DownloadJob, target_path
from portolan_registry_qgis.qgis_io.network import (
    NetworkError,
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


def test_archive_header_and_tiles(catalog):
    archive = open_archive(f"{catalog['base']}/points.pmtiles")
    assert archive.min_zoom == 0
    assert archive.max_zoom == 8
    assert archive.vector_layers == ("points",)
    west, south, east, north = archive.bounds
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


def test_style_split_and_sources():
    style = {
        "version": 8,
        "sources": {
            "a": {"type": "vector", "url": "pmtiles://../a.pmtiles"},
            "b": {"type": "vector", "url": "pmtiles://https://x.test/b.pmtiles"},
            "bare": {"type": "vector", "url": "../d.pmtiles?v=2"},
            "tilejson": {"type": "vector", "url": "https://tiles.test/v.json"},
            "xyz": {"type": "vector", "tiles": ["https://tiles.test/{z}/{x}/{y}.pbf"]},
            "base": {"type": "raster", "tiles": ["https://tiles.test/{z}/{x}/{y}.png"]},
        },
        "layers": [
            {"id": "bg", "type": "background"},
            {"id": "la", "type": "fill", "source": "a", "source-layer": "l"},
            {"id": "lb", "type": "line", "source": "b", "source-layer": "l"},
            {"id": "sat", "type": "raster", "source": "base"},
        ],
    }
    url = "https://x.test/c/styles/s.json"
    assert layers.pmtiles_sources(style, url) == {
        "a": "https://x.test/c/a.pmtiles",
        "b": "https://x.test/b.pmtiles",
        "bare": "https://x.test/c/d.pmtiles?v=2",
    }
    split = layers.split_style(style, url)
    assert {k: [layer["id"] for layer in v["layers"]] for k, v in split.by_source.items()} == {
        "https://x.test/c/a.pmtiles": ["bg", "la"],
        "https://x.test/b.pmtiles": ["lb"],
    }
    assert len(split.warnings) == 1
    assert "sat" in split.warnings[0]


def test_source_without_url_reads_the_only_archive():
    style = {"version": 8, "sources": {"data": {"type": "vector"}}, "layers": []}
    url = "https://x.test/c/styles/s.json"
    assert layers.pmtiles_sources(style, url) == {}
    assert layers.pmtiles_sources(style, url, "https://x.test/c/a.pmtiles") == {
        "data": "https://x.test/c/a.pmtiles"
    }


def test_parse_style_rejects_non_styles():
    assert layers.parse_style(b'{"version": 8, "layers": []}')["version"] == 8
    for raw in (b"[]", b'{"version": 7}'):
        with pytest.raises(ValueError, match="MapLibre"):
            layers.parse_style(raw)


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


def _red_pixels(image: QImage) -> int:
    count = 0
    for x in range(0, image.width(), 2):
        for y in range(0, image.height(), 2):
            color = image.pixelColor(x, y)
            if color.red() > 200 and color.green() < 80 and color.blue() < 80:
                count += 1
    return count


# Web Mercator extent around the points near Bologna.
BOLOGNA = QgsRectangle(1_200_000, 5_440_000, 1_500_000, 5_640_000)


def _blue_pixels(image: QImage) -> int:
    count = 0
    for x in range(0, image.width(), 2):
        for y in range(0, image.height(), 2):
            color = image.pixelColor(x, y)
            if color.blue() > 200 and color.red() < 80 and color.green() < 80:
                count += 1
    return count


def test_tiles_carry_every_style_as_a_named_style(catalog, collection, server):
    urls = layers.archive_urls(collection)
    assert urls == [f"{catalog['base']}/points.pmtiles"]
    prepared = layers.prepare_pmtiles(collection, urls, fetch_bytes)
    assert prepared.warnings == []
    built, _ = layers.build_pmtiles(server, prepared, QImage.fromData)
    (layer,) = built
    assert layer.isValid()
    assert layer.name() == "Point tiles"
    assert layer.customProperty(layers.PMTILES_PROPERTY).endswith("/points.pmtiles")
    manager = layer.styleManager()
    assert sorted(manager.styles()) == sorted(
        [layers.DEFAULT_STYLE_NAME, "style-red", "style-blue"]
    )
    # The default style is current, and each named style draws its own color.
    assert manager.currentStyle() == "style-red"
    image = _render(layer, BOLOGNA)
    assert _red_pixels(image) > 0
    assert _blue_pixels(image) == 0
    manager.setCurrentStyle("style-blue")
    image = _render(layer, BOLOGNA)
    assert _blue_pixels(image) > 0
    assert _red_pixels(image) == 0
    manager.setCurrentStyle(layers.DEFAULT_STYLE_NAME)
    assert _red_pixels(_render(layer, BOLOGNA)) == 0
    manager.setCurrentStyle("style-red")
    assert _red_pixels(_render(layer, BOLOGNA)) > 0


def test_tiles_without_styles_use_the_qgis_style(catalog, collection, server):
    bare = replace(collection, assets=collection.data_assets)
    urls = layers.archive_urls(bare)
    prepared = layers.prepare_pmtiles(bare, urls, fetch_bytes)
    assert prepared.warnings == []
    built, _ = layers.build_pmtiles(server, prepared, QImage.fromData)
    (layer,) = built
    assert layer.styleManager().styles() == [layers.DEFAULT_STYLE_NAME]


def test_unreadable_style_is_skipped_with_a_warning(collection, server):
    red, blue = collection.styles
    broken = replace(red, href=red.href.replace("red", "gone"))
    others = [a for a in collection.assets if a.key != red.key]
    document = replace(collection, assets=(broken, *others))
    urls = layers.archive_urls(document)
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


def _parquet_url(catalog):
    return f"{catalog['base']}/points.parquet"


def _load(url, extent=None, extent_crs=None):
    context = QgsProject.instance().transformContext()
    planned = parquet_layer.plan(url, context, extent, extent_crs)
    prepared = parquet_layer.prepare(planned)
    return planned, prepared


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
    assert [path for path, _ in report.failed] == ["catalog.json", "missing.json"]
    assert "do not match" in report.failed[0][1]
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
