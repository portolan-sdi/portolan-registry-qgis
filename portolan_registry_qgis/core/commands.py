"""Write the GDAL shell commands that repeat a load outside QGIS.

Each argument is quoted for a POSIX shell.
"""

from __future__ import annotations

import shlex
from urllib.parse import urlsplit

from portolan_registry_qgis.core.hrefs import plain_vsicurl_path

# The files that the mosaic commands write.
MOSAIC_FILES = "mosaic-files.txt"  # i18n: skip - a file name
CUTLINE = "clip.geojson"  # i18n: skip - a file name
MOSAIC = "mosaic.vrt"  # i18n: skip - a file name
CLIPPED = "mosaic-clipped.vrt"  # i18n: skip - a file name


def _line(*args: str) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def file_name(url: str, extension: str) -> str:
    """Return the local file name for a copy of ``url``, with ``extension`` in place of its own."""
    name = urlsplit(url).path.rsplit("/", 1)[-1] or "data"
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}.{extension}"


def translate_command(url: str) -> str:
    """Return the ``gdal_translate`` command that copies the raster at ``url`` to a GeoTIFF."""
    return _line("gdal_translate", plain_vsicurl_path(url), file_name(url, "tif")) + "\n"


def ogr2ogr_command(url: str) -> str:
    """Return the ``ogr2ogr`` command that copies the vector file at ``url`` to a GeoPackage."""
    return _line("ogr2ogr", file_name(url, "gpkg"), plain_vsicurl_path(url)) + "\n"


def _here_document(path: str, text: str) -> list[str]:
    """Return the lines that write ``text`` to ``path`` with a quoted here-document."""
    return [f"cat > {shlex.quote(path)} <<'EOF'", text, "EOF"]


def mosaic_commands(urls: list[str], cutline: str | None = None) -> str:
    """Return the commands that build one VRT over the COGs at ``urls``.

    ``cutline`` is a GeoJSON FeatureCollection in longitude and latitude. With
    it, ``gdalwarp`` clips and crops the mosaic, as "Add as one layer" does.
    """
    paths = "\n".join(plain_vsicurl_path(url) for url in urls)
    lines = [
        *_here_document(MOSAIC_FILES, paths),
        _line("gdalbuildvrt", "-input_file_list", MOSAIC_FILES, MOSAIC),
    ]
    if cutline is not None:
        lines += _here_document(CUTLINE, cutline)
        lines.append(
            _line(
                "gdalwarp",
                "-of",
                "VRT",
                "-cutline",
                CUTLINE,
                "-crop_to_cutline",
                "-dstalpha",
                MOSAIC,
                CLIPPED,
            )
        )
    return "\n".join(lines) + "\n"
