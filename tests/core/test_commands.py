from __future__ import annotations

import shlex

from portolan_registry_qgis.core.commands import (
    file_name,
    mosaic_commands,
    ogr2ogr_command,
    translate_command,
)


def test_file_name_replaces_the_extension():
    assert file_name("https://x.org/a/dem.tif?sig=1", "tif") == "dem.tif"
    assert file_name("https://x.org/a/roads.fgb", "gpkg") == "roads.gpkg"
    assert file_name("https://x.org/", "gpkg") == "data.gpkg"


def test_translate_command_reads_over_vsicurl():
    assert translate_command("https://x.org/dem.tif") == (
        "gdal_translate /vsicurl/https://x.org/dem.tif dem.tif\n"
    )


def test_ogr2ogr_command_quotes_each_argument():
    url = "https://x.org/a b/it's.geojson?k=1&v=2"
    words = shlex.split(ogr2ogr_command(url))
    assert words == ["ogr2ogr", "it's.gpkg", f"/vsicurl/{url}"]


def test_mosaic_without_a_clip_builds_one_vrt():
    text = mosaic_commands(["https://x.org/a.tif", "https://x.org/b.tif"])
    assert text == (
        "cat > mosaic-files.txt <<'EOF'\n"
        "/vsicurl/https://x.org/a.tif\n"
        "/vsicurl/https://x.org/b.tif\n"
        "EOF\n"
        "gdalbuildvrt -input_file_list mosaic-files.txt mosaic.vrt\n"
    )


def test_mosaic_with_a_clip_writes_the_cutline_and_warps():
    cutline = '{"type": "FeatureCollection", "features": []}'
    lines = mosaic_commands(["https://x.org/a.tif"], cutline).splitlines()
    assert lines[4:7] == ["cat > clip.geojson <<'EOF'", cutline, "EOF"]
    assert lines[7] == (
        "gdalwarp -of VRT -cutline clip.geojson -crop_to_cutline -dstalpha "
        "mosaic.vrt mosaic-clipped.vrt"
    )
