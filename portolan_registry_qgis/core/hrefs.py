"""Resolve STAC hrefs to URLs that QGIS and GDAL can read.

Ported from GeoLibre's ``stac-api.ts`` (MIT, see NOTICE): ``absoluteHref``,
``browserCatalogHref``, and ``browserAssetHref``.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

_S3_WEBSITE = re.compile(r"^(.+)\.s3-website[.-]([a-z0-9-]+)\.amazonaws\.com$", re.IGNORECASE)
_DFS_SUFFIX = ".dfs.core.windows.net"  # i18n: skip - a host
_AZURE_SCHEMES = frozenset({"abfs", "abfss", "az"})
_GLOB = re.compile(r"[*\[]")
_GCS_HOST = "storage.googleapis.com"  # i18n: skip - a host
# bucket.s3.amazonaws.com, bucket.s3.region.amazonaws.com, bucket.s3-region.amazonaws.com
_S3_VIRTUAL = re.compile(r"^(.+)\.s3(?:[.-]([a-z0-9-]+))?\.amazonaws\.com$", re.IGNORECASE)
# s3.amazonaws.com, s3.region.amazonaws.com, s3-region.amazonaws.com
_S3_PATH = re.compile(r"^s3(?:[.-]([a-z0-9-]+))?\.amazonaws\.com$", re.IGNORECASE)
_REGION_PREFIX = re.compile(r"^([a-z]{2}(?:-gov)?-[a-z]+-\d)\.")


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
        netloc, path = f"s3.{region}.amazonaws.com", f"/{bucket}{parts.path}"  # i18n: skip - a host
    else:
        netloc, path = f"{bucket}.s3.{region}.amazonaws.com", parts.path  # i18n: skip - a host
    return urlunsplit(("https", netloc, path, parts.query, parts.fragment))


def absolute_href(href: str, base: str) -> str:
    """Resolve ``href`` against ``base`` and upgrade S3 website endpoints."""
    return catalog_href(urljoin(base, href))


def asset_href(href: str, base: str, account_name: str | None = None) -> str:
    """Resolve an asset href to HTTPS, converting anonymous object-store URIs.

    Azure shorthand hrefs need ``account_name``. Without it, the href comes
    back unchanged.
    """
    resolved = absolute_href(href, base)
    parts = urlsplit(resolved)
    tail = urlunsplit(("", "", parts.path, parts.query, parts.fragment))
    if parts.scheme == "s3":
        return _s3_https(parts.hostname, tail) if parts.hostname else resolved
    if parts.scheme == "gs":
        return f"https://{_GCS_HOST}/{parts.hostname}{tail}" if parts.hostname else resolved
    if parts.scheme in _AZURE_SCHEMES:
        return _azure_https(parts.hostname or "", parts.username, tail, account_name) or resolved
    return resolved


def _azure_https(host: str, user: str | None, tail: str, account_name: str | None) -> str | None:
    """Return the blob HTTPS URL of an Azure URI, or None without a container and an account."""
    if host.endswith(_DFS_SUFFIX):
        container = unquote(user or "")
        account: str | None = host[: -len(_DFS_SUFFIX)]
    else:
        container, account = host, account_name
    if not container or not account:
        return None
    return f"https://{account}.blob.core.windows.net/{container}{tail}"  # i18n: skip - a URL


def bucket_region(bucket: str) -> str | None:
    """Return the AWS region that a bucket name starts with, such as ``us-west-2``."""
    match = _REGION_PREFIX.match(bucket)
    return match.group(1) if match else None


def _s3_https(bucket: str, tail: str) -> str:
    """Return the HTTPS URL of an S3 object. A dotted bucket uses the path style."""
    if "." not in bucket:
        return f"https://{bucket}.s3.amazonaws.com{tail}"  # i18n: skip - a URL
    region = bucket_region(bucket)
    host = f"s3.{region}.amazonaws.com" if region else "s3.amazonaws.com"  # i18n: skip - a host
    return f"https://{host}/{bucket}{tail}"


def is_glob(href: str) -> bool:
    """Return whether the path of ``href`` holds ``*`` or ``[``. A ``?`` starts the query."""
    return bool(_GLOB.search(urlsplit(href).path))


def glob_root(href: str) -> str:
    """Return ``href`` up to the folder before its first path segment with a glob character."""
    parts = urlsplit(href)
    kept = []
    for segment in parts.path.split("/"):
        if _GLOB.search(segment):
            break
        kept.append(segment)
    return urlunsplit((parts.scheme, parts.netloc, "/".join(kept), "", ""))


def bucket_url(href: str) -> str | None:
    """Return the ``s3://`` or ``gs://`` form of ``href``, which DuckDB can list, or None.

    HTTPS has no listing, so DuckDB expands a glob only in a bucket URL.
    """
    parts = urlsplit(href)
    host, path = (parts.hostname or "").lower(), parts.path
    if parts.scheme in {"s3", "gs"}:
        return href if host else None
    if parts.scheme != "https" or not host:
        return None
    return _https_bucket(host, path)


def _https_bucket(host: str, path: str) -> str | None:
    """Return the bucket URL of a GCS or S3 HTTPS endpoint, or None for another host."""
    has_bucket = path.count("/") > 1
    if host == _GCS_HOST:
        return f"gs:/{path}" if has_bucket else None
    if _S3_PATH.match(host):
        return f"s3:/{path}" if has_bucket else None
    virtual = _S3_VIRTUAL.match(host)
    return f"s3://{virtual.group(1)}{path}" if virtual else None


def vsicurl_path(url: str) -> str:
    """Return the GDAL path that reads ``url`` over HTTP range requests.

    ``empty_dir=yes`` stops side-car probes, so external ``.ovr`` files are
    ignored. GDAL refuses this form when ``CPL_VSIL_CURL_ALLOWED_EXTENSIONS``
    is set. Use ``plain_vsicurl_path`` then.
    """
    return f"/vsicurl?empty_dir=yes&url={quote(url, safe='')}"  # i18n: skip - a GDAL path


def plain_vsicurl_path(url: str) -> str:
    """Return the GDAL path that reads ``url`` with no ``/vsicurl`` options."""
    return f"/vsicurl/{url}"


def url_extension(url: str) -> str:
    """Return the extension of the file a URL names, in lower case, without the dot."""
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def allowed_extensions(setting: str) -> tuple[str, ...]:
    """Return the entries of ``CPL_VSIL_CURL_ALLOWED_EXTENSIONS``.

    GDAL splits the value at commas and spaces, and compares each entry with
    the end of the file name, ignoring case.
    """
    return tuple(entry for entry in setting.replace(",", " ").split() if entry)


def extension_blocked(url: str, setting: str | None) -> bool:
    """Return whether ``CPL_VSIL_CURL_ALLOWED_EXTENSIONS`` keeps GDAL from reading ``url``."""
    if not setting:
        return False
    name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    return not any(name.endswith(entry.lower()) for entry in allowed_extensions(setting))


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


BROWSER_URL = "https://browser.portolan-sdi.org/"  # i18n: skip - a URL


def browser_url(stac_href: str, base: str = BROWSER_URL) -> str:
    """Return the Portolan browser page that shows the STAC document at ``stac_href``.

    Raises:
        ValueError: ``stac_href`` is not an http(s) URL.
    """
    if not is_http_url(stac_href):
        raise ValueError(f"Not an http(s) URL: {stac_href}")  # i18n: skip - the window catches it
    target = urlsplit(stac_href)
    parts = ["/external"]
    if target.scheme != "https":
        parts.append(f"{target.scheme}:")
    parts += [target.netloc, target.path.removeprefix("/")]
    route = "/".join(parts)
    if target.query:
        route += f"?{target.query}"
    root = urlsplit(base)
    path = root.path if root.path.endswith("/") else root.path + "/"
    return urlunsplit((root.scheme, root.netloc, path, root.query, route))
