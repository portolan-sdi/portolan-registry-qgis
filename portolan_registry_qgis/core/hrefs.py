"""Resolve STAC hrefs to URLs that QGIS and GDAL can read.

Ported from GeoLibre's ``stac-api.ts`` (MIT, see NOTICE): ``absoluteHref``,
``browserCatalogHref``, and ``browserAssetHref``.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

_S3_WEBSITE = re.compile(r"^(.+)\.s3-website[.-]([a-z0-9-]+)\.amazonaws\.com$", re.IGNORECASE)
_DFS_SUFFIX = ".dfs.core.windows.net"
_AZURE_SCHEMES = frozenset({"abfs", "abfss", "az"})


def is_http_url(value: object) -> bool:
    """Return whether ``value`` is an absolute http or https URL with a host."""
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def catalog_href(href: str) -> str:
    """Rewrite an S3 website endpoint to the equivalent HTTPS REST endpoint.

    S3 website endpoints serve HTTP only. A bucket whose name holds a dot goes
    through the path-style endpoint, because the virtual-hosted certificate
    covers one label.
    """
    parts = urlsplit(href)
    match = _S3_WEBSITE.match(parts.hostname or "")
    if not match:
        return href
    bucket, region = match.group(1), match.group(2)
    if "." in bucket:
        netloc, path = f"s3.{region}.amazonaws.com", f"/{bucket}{parts.path}"
    else:
        netloc, path = f"{bucket}.s3.{region}.amazonaws.com", parts.path
    return urlunsplit(("https", netloc, path, parts.query, parts.fragment))


def absolute_href(href: str, base: str) -> str:
    """Resolve ``href`` against ``base`` and upgrade S3 website endpoints."""
    return catalog_href(urljoin(base, href))


def asset_href(href: str, base: str, account_name: str | None = None) -> str:
    """Resolve an asset href to HTTPS, converting anonymous object-store URIs.

    ``s3://bucket/key`` becomes the bucket's virtual-hosted HTTPS URL. Azure
    ``abfs``/``abfss``/``az`` hrefs name the container first; the canonical form
    carries the account in the host, and the shorthand relies on
    ``account_name`` from the asset's ``table:storage_options``. Without an
    account the href comes back unchanged.
    """
    resolved = absolute_href(href, base)
    parts = urlsplit(resolved)
    tail = urlunsplit(("", "", parts.path, parts.query, parts.fragment))
    if parts.scheme == "s3":
        bucket = parts.hostname
        return f"https://{bucket}.s3.amazonaws.com{tail}" if bucket else resolved
    if parts.scheme in _AZURE_SCHEMES:
        host = parts.hostname or ""
        if host.endswith(_DFS_SUFFIX):
            container = unquote(parts.username or "")
            account: str | None = host[: -len(_DFS_SUFFIX)]
        else:
            container, account = host, account_name
        if not container or not account:
            return resolved
        return f"https://{account}.blob.core.windows.net/{container}{tail}"
    return resolved


def vsicurl_path(url: str) -> str:
    """Return the GDAL path that reads ``url`` over HTTP range requests.

    The ``empty_dir=yes`` option stops GDAL from listing the parent directory
    and from probing side-car files such as ``.aux.xml`` and ``.ovr``. A
    catalog asset has no side-car files, so those requests only add latency.
    GDAL needs the URL percent-encoded in this form, query string included.
    """
    return f"/vsicurl?empty_dir=yes&url={quote(url, safe='')}"


def folder_name(href: str) -> str:
    """Name an untitled catalog node after the folder it sits in.

    A ``.json`` file names its parent folder. A JSON file at the root has no
    folder, so its own stem is used.
    """
    segments = [segment for segment in urlsplit(href).path.split("/") if segment]
    if not segments:
        return href
    last = segments[-1]
    if last.lower().endswith(".json"):
        name = segments[-2] if len(segments) > 1 else last[: -len(".json")]
    else:
        name = last
    return unquote(name)
