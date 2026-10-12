from __future__ import annotations

from portolan_registry_qgis.core.diagnose import gdal_reason, open_failure

URL = "https://data.source.coop/nlebovits/ghsl/pop-2030/tile.tif"
# The text QGIS 3.44 gives for a missing codec and for a missing file.
LERC = (
    f"Cannot open GDAL dataset /vsicurl/{URL}:\n"
    "tile.tif: Cannot open TIFF file due to missing codec LERC."
)
GONE = f"Cannot open GDAL dataset /vsicurl/{URL}:\n"


def test_gdal_reason_drops_the_qgis_wrapper():
    assert gdal_reason(LERC) == "tile.tif: Cannot open TIFF file due to missing codec LERC."
    assert gdal_reason(GONE) == ""
    assert gdal_reason("Provider is not valid (provider: gdal, URI: x)") == ""


def test_missing_codec_in_a_flatpak_says_what_to_do():
    message = open_failure(URL, LERC, flatpak=True)
    assert "uses LERC compression" in message
    assert "QGIS Flatpak builds GDAL without LERC" in message


def test_missing_codec_elsewhere():
    message = open_failure(URL, "x.tif: ZSTD compression support is not configured", False)
    assert "uses ZSTD compression" in message
    assert message.endswith("Open it in a QGIS whose GDAL includes ZSTD.")


def test_other_failures_pass_gdal_on_or_stay_short():
    assert open_failure(URL, "HTTP response code: 403", False) == (
        f"QGIS could not open {URL}: HTTP response code: 403"
    )
    assert open_failure(URL, GONE, False) == f"QGIS could not open {URL}"


def test_gdal_reason_drops_the_translated_qgis_wrapper():
    # QGIS in Spanish translates the provider line, but not the GDAL prefix.
    detail = (
        f"Cannot open GDAL dataset /vsicurl/{URL}:\n"
        "tile.tif: HTTP response code: 404\n"
        "El proveedor no es válido (proveedor: gdal, URL: x)"
    )
    invalid = "El proveedor no es válido (proveedor: "
    assert gdal_reason(detail, invalid) == "tile.tif: HTTP response code: 404"
    assert "El proveedor" in gdal_reason(detail)
    assert open_failure(URL, detail, False, invalid).endswith("HTTP response code: 404")
