"""Href resolution. Cases ported from GeoLibre's tests/stac-api.test.ts (MIT)."""

from __future__ import annotations

import pytest

from portolan_registry_qgis.core.hrefs import (
    BROWSER_URL,
    absolute_href,
    asset_href,
    browser_url,
    catalog_href,
    extension_blocked,
    folder_name,
    is_http_url,
    plain_vsicurl_path,
    url_extension,
    vsicurl_path,
)


def test_s3_assets_become_https():
    assert (
        asset_href("s3://public-bucket/path/to/data.tif", "https://example.com/catalog/")
        == "https://public-bucket.s3.amazonaws.com/path/to/data.tif"
    )
    assert (
        asset_href("./data.tif", "https://example.com/catalog/item.json")
        == "https://example.com/catalog/data.tif"
    )


def test_azure_hrefs_resolve_against_the_named_account():
    assert (
        asset_href(
            "abfs://us-census/2020/cb_2020_us_state_500k.parquet",
            "https://planetarycomputer.microsoft.com/api/stac/v1/",
            "ai4edataeuwest",
        )
        == "https://ai4edataeuwest.blob.core.windows.net/us-census/2020/cb_2020_us_state_500k.parquet"
    )
    assert (
        asset_href("az://container/a.parquet", "https://example.com/", "acct")
        == "https://acct.blob.core.windows.net/container/a.parquet"
    )
    # Without an account there is nothing to resolve against.
    assert (
        asset_href("abfs://us-census/2020/x.parquet", "https://example.com/")
        == "abfs://us-census/2020/x.parquet"
    )


def test_canonical_abfs_names_its_own_account():
    assert (
        asset_href("abfss://container@acct.dfs.core.windows.net/dir/a.parquet", "https://x.test/")
        == "https://acct.blob.core.windows.net/container/dir/a.parquet"
    )
    # An account named beside it does not override the one the URI states.
    assert (
        asset_href("abfs://container@acct.dfs.core.windows.net/a.parquet", "https://x.test/", "o")
        == "https://acct.blob.core.windows.net/container/a.parquet"
    )
    # The canonical host with no container cannot resolve.
    assert (
        asset_href("abfss://acct.dfs.core.windows.net/a.parquet", "https://x.test/")
        == "abfss://acct.dfs.core.windows.net/a.parquet"
    )


def test_s3_website_catalogs_upgrade_to_https():
    assert (
        catalog_href("http://example.s3-website-us-west-2.amazonaws.com/catalog.json")
        == "https://example.s3.us-west-2.amazonaws.com/catalog.json"
    )


def test_dotted_bucket_uses_path_style_endpoint():
    assert (
        catalog_href("http://example.catalog.s3-website-us-west-2.amazonaws.com/catalog.json")
        == "https://s3.us-west-2.amazonaws.com/example.catalog/catalog.json"
    )


def test_absolute_href_upgrades_after_joining():
    assert (
        absolute_href("catalog.json", "http://b.s3-website.eu-west-1.amazonaws.com/x/")
        == "https://b.s3.eu-west-1.amazonaws.com/x/catalog.json"
    )


@pytest.mark.parametrize(
    ("href", "name"),
    [
        ("https://example.com/stac/maps/collection.json", "maps"),
        # A bare % is legal in a path. The raw name beats no name.
        ("https://example.com/stac/100%_coverage/catalog.json", "100%_coverage"),
        ("https://example.com/stac/UPPER/CATALOG.JSON", "UPPER"),
        ("https://example.com/stac/quads/", "quads"),
        # At the root there is no folder to borrow a name from.
        ("https://example.com/standalone.json", "standalone"),
        ("https://example.com/a%20b/catalog.json", "a b"),
        ("https://example.com/", "https://example.com/"),
    ],
)
def test_folder_name(href, name):
    assert folder_name(href) == name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/a", True),
        ("http://example.com", True),
        ("javascript:alert(1)", False),
        ("https://", False),
        ("s3://bucket/key", False),
        (None, False),
        (42, False),
        ("http://[::1", False),
    ],
)
def test_is_http_url(value, expected):
    assert is_http_url(value) is expected


@pytest.mark.parametrize(
    ("url", "path"),
    [
        (
            "https://example.com/a/relief.tif",
            "/vsicurl?empty_dir=yes&url=https%3A%2F%2Fexample.com%2Fa%2Frelief.tif",
        ),
        # A signed URL keeps its query string inside the encoded url option.
        (
            "https://b.test/x%20y.fgb?X-Amz-Signature=a%2Fb&e=1",
            (
                "/vsicurl?empty_dir=yes&url="
                "https%3A%2F%2Fb.test%2Fx%2520y.fgb%3FX-Amz-Signature%3Da%252Fb%26e%3D1"
            ),
        ),
    ],
)
def test_vsicurl_path_skips_directory_listing(url, path):
    assert vsicurl_path(url) == path


def test_plain_vsicurl_path():
    url = "https://example.com/a/relief.tif?x=1"
    assert plain_vsicurl_path(url) == "/vsicurl/https://example.com/a/relief.tif?x=1"


@pytest.mark.parametrize(
    ("url", "setting", "blocked"),
    [
        # A QGIS 3.40 session set the value TIF, which blocks GeoParquet.
        ("https://x.test/a/b.parquet", "TIF", True),
        ("https://x.test/a/b.tif", "TIF", False),
        ("https://x.test/a/b.TIF?sig=1", ".tif", False),
        ("https://x.test/a/b.fgb", ".tif, .fgb", False),
        ("https://x.test/a/b.fgb", ".tif .geojson", True),
        ("https://x.test/a/b.fgb", None, False),
        ("https://x.test/a/b.fgb", "", False),
    ],
)
def test_extension_blocked(url, setting, blocked):
    assert extension_blocked(url, setting) is blocked


def test_url_extension():
    assert url_extension("https://x.test/a/B.TIF?x=y.z") == "tif"
    assert url_extension("https://x.test/a/noext") == ""


# The expected paths follow toBrowserPath in portolan-browser's
# src/store/index.js, under the hash routing that browser.portolan-sdi.org uses.
@pytest.mark.parametrize(
    ("href", "url"),
    [
        (
            "https://data.example.org/stac/roads/collection.json",
            "https://browser.portolan-sdi.org/#/external/data.example.org/stac/roads/collection.json",
        ),
        # A document not on HTTPS names its scheme before the host.
        (
            "http://data.example.org:8080/catalog.json",
            "https://browser.portolan-sdi.org/#/external/http:/data.example.org:8080/catalog.json",
        ),
        # The query string follows the path. The fragment is dropped.
        (
            "https://b.test/c/item.json?sig=a%2Fb&e=1#top",
            "https://browser.portolan-sdi.org/#/external/b.test/c/item.json?sig=a%2Fb&e=1",
        ),
        # Percent-encoding in the path stays as it is.
        (
            "https://b.test/my%20data/catalog.json",
            "https://browser.portolan-sdi.org/#/external/b.test/my%20data/catalog.json",
        ),
        ("https://b.test", "https://browser.portolan-sdi.org/#/external/b.test/"),
    ],
)
def test_browser_url(href, url):
    assert browser_url(href) == url
    assert browser_url(href, BROWSER_URL) == url


@pytest.mark.parametrize(
    "base",
    [
        "https://mirror.test/browser",
        "https://mirror.test/browser/",
        "https://mirror.test/browser/#/",
    ],
)
def test_browser_url_on_a_mirror(base):
    assert (
        browser_url("https://b.test/catalog.json", base)
        == "https://mirror.test/browser/#/external/b.test/catalog.json"
    )


def test_browser_url_on_a_bare_host():
    assert browser_url("https://b.test/c.json", "http://localhost:8080") == (
        "http://localhost:8080/#/external/b.test/c.json"
    )


@pytest.mark.parametrize("href", ["s3://bucket/catalog.json", "./catalog.json", ""])
def test_browser_url_needs_an_http_url(href):
    with pytest.raises(ValueError, match="http"):
        browser_url(href)
