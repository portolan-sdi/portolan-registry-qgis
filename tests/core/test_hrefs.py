"""Href resolution. Cases ported from GeoLibre's tests/stac-api.test.ts (MIT)."""

from __future__ import annotations

import pytest

from portolan_registry_qgis.core.hrefs import (
    BROWSER_URL,
    absolute_href,
    asset_href,
    browser_url,
    bucket_region,
    bucket_url,
    catalog_href,
    extension_blocked,
    folder_name,
    glob_root,
    is_glob,
    is_http_url,
    plain_vsicurl_path,
    url_extension,
    vsicurl_path,
)

_ABFS = "https://ai4edataeuwest.blob.core.windows.net/us-census/2020/cb_2020_us_state_500k.parquet"


@pytest.mark.parametrize(
    ("href", "base", "account", "expected"),
    [
        pytest.param(
            "s3://public-bucket/path/to/data.tif",
            "https://example.com/catalog/",
            None,
            "https://public-bucket.s3.amazonaws.com/path/to/data.tif",
            id="s3-becomes-https",
        ),
        pytest.param(
            "./data.tif",
            "https://example.com/catalog/item.json",
            None,
            "https://example.com/catalog/data.tif",
            id="relative",
        ),
        pytest.param(
            "abfs://us-census/2020/cb_2020_us_state_500k.parquet",
            "https://planetarycomputer.microsoft.com/api/stac/v1/",
            "ai4edataeuwest",
            _ABFS,
            id="abfs-named-account",
        ),
        pytest.param(
            "az://container/a.parquet",
            "https://example.com/",
            "acct",
            "https://acct.blob.core.windows.net/container/a.parquet",
            id="az-named-account",
        ),
        # Without an account there is nothing to resolve against.
        pytest.param(
            "abfs://us-census/2020/x.parquet",
            "https://example.com/",
            None,
            "abfs://us-census/2020/x.parquet",
            id="abfs-no-account",
        ),
        pytest.param(
            "abfss://container@acct.dfs.core.windows.net/dir/a.parquet",
            "https://x.test/",
            None,
            "https://acct.blob.core.windows.net/container/dir/a.parquet",
            id="canonical-abfss",
        ),
        # An account named beside it does not override the one the URI states.
        pytest.param(
            "abfs://container@acct.dfs.core.windows.net/a.parquet",
            "https://x.test/",
            "o",
            "https://acct.blob.core.windows.net/container/a.parquet",
            id="canonical-abfs-keeps-its-account",
        ),
        # The canonical host with no container cannot resolve.
        pytest.param(
            "abfss://acct.dfs.core.windows.net/a.parquet",
            "https://x.test/",
            None,
            "abfss://acct.dfs.core.windows.net/a.parquet",
            id="canonical-no-container",
        ),
    ],
)
def test_asset_href(href, base, account, expected):
    assert asset_href(href, base, account) == expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        pytest.param(
            "http://example.s3-website-us-west-2.amazonaws.com/catalog.json",
            "https://example.s3.us-west-2.amazonaws.com/catalog.json",
            id="s3-website-upgrades",
        ),
        pytest.param(
            "http://example.catalog.s3-website-us-west-2.amazonaws.com/catalog.json",
            "https://s3.us-west-2.amazonaws.com/example.catalog/catalog.json",
            id="dotted-bucket-path-style",
        ),
    ],
)
def test_catalog_href(href, expected):
    assert catalog_href(href) == expected


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


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("gs://bucket/c/d.parquet", "https://storage.googleapis.com/bucket/c/d.parquet"),
        ("s3://my.bucket/d.parquet", "https://s3.amazonaws.com/my.bucket/d.parquet"),
        (
            "s3://us-west-2.data.test/d.parquet",
            "https://s3.us-west-2.amazonaws.com/us-west-2.data.test/d.parquet",
        ),
        (
            "s3://us-west-2.opendata.source.coop/a/b.parquet",
            "https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/a/b.parquet",
        ),
    ],
)
def test_asset_href_of_gcs_and_dotted_s3_buckets(href, expected):
    assert asset_href(href, "https://x.test/c/collection.json") == expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://x.test/c/*.parquet", True),
        ("https://x.test/c/zone=?/a.parquet", False),
        ("https://x.test/c/[ab].parquet", True),
        ("https://x.test/c/a.parquet?v=*", False),
        ("https://x.test/c/a.parquet", False),
    ],
)
def test_is_glob_reads_the_path_only(href, expected):
    assert is_glob(href) is expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("s3://b/k/*.parquet", "s3://b/k/*.parquet"),
        ("gs://b/k/*.parquet", "gs://b/k/*.parquet"),
        ("https://data.source.coop/a/r/*.parquet", None),
        ("https://storage.googleapis.com/b/k/*.parquet", "gs://b/k/*.parquet"),
        ("https://b.s3.amazonaws.com/k/*.parquet", "s3://b/k/*.parquet"),
        ("https://b.s3.eu-west-1.amazonaws.com/k/*", "s3://b/k/*"),
        ("https://s3.us-west-2.amazonaws.com/my.bucket/k/*", "s3://my.bucket/k/*"),
        ("https://s3.amazonaws.com/onlybucket", None),
        ("https://storage.googleapis.com/onlybucket", None),
        ("https://pub-1.r2.dev/t/*.parquet", None),
        ("http://b.s3.amazonaws.com/k/*", None),
        ("s3:///k", None),
    ],
)
def test_bucket_url(href, expected):
    assert bucket_url(href) == expected


def test_asset_href_and_bucket_url_round_trip():
    for href in ("s3://b/k/*.parquet", "gs://b/k/*.parquet", "s3://my.bucket/k/*"):
        assert bucket_url(asset_href(href, "https://x.test/")) == href


def test_bucket_region():
    assert bucket_region("us-west-2.opendata.source.coop") == "us-west-2"
    assert bucket_region("us-gov-west-1.data") == "us-gov-west-1"
    assert bucket_region("my.bucket") is None


@pytest.mark.parametrize(
    ("href", "root"),
    [
        ("https://x.test/c/2020/zone=*/utm*.parquet", "https://x.test/c/2020"),
        ("https://x.test/c/*/b_*.parquet", "https://x.test/c"),
        ("s3://b/k/*.parquet", "s3://b/k"),
        ("https://x.test/c/a.parquet", "https://x.test/c/a.parquet"),
    ],
)
def test_glob_root(href, root):
    assert glob_root(href) == root
