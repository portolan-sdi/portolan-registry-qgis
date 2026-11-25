"""Property tests for href resolution against RFC 3986."""

from __future__ import annotations

import operator
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portolan_registry_qgis.core import hrefs
from tests.property import strategies

# RFC 3986 Appendix B: the regular expression that splits a URI reference.
_URI = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?$", re.DOTALL)
_SEGMENT = re.compile(r"/?[^/]*")
# absolute_href uses urllib.parse.urljoin, which differs from RFC 3986 in four
# ways. The strategies leave these inputs out:
# - It drops an empty segment when it merges a relative path.
# - It drops an empty query "?" and an empty fragment "#".
# - It drops a ";" at the end of the path, an empty RFC 1808 parameter.
# - It keeps the dot segments of an href with an authority.
# test_resolving_an_absolute_href_is_idempotent pins the last difference.
# Path characters of RFC 3986 section 3.3, without ":" so no first segment reads as a scheme.
_PCHARS = strategies.UNRESERVED + "!$&'()*+,;=@"
Parts = tuple[str | None, str | None, str, str | None, str | None]


def _split(reference: str) -> Parts:
    match = _URI.match(reference)
    assert match is not None
    return match.group(2), match.group(4), match.group(5), match.group(7), match.group(9)


def _dot_step(path: str, out: list[str]) -> str:
    """Return the input left after one pass of the loop in RFC 3986 section 5.2.4."""
    for prefix in ("../", "./"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    if path.startswith("/./") or path == "/.":
        return "/" + path[3:]
    if path.startswith("/../") or path == "/..":
        if out:
            out.pop()
        return "/" + path[4:]
    if path in {".", ".."}:
        return ""
    match = _SEGMENT.match(path)
    assert match is not None
    out.append(match.group())
    return path[match.end() :]


def _remove_dots(path: str) -> str:
    """Return ``path`` after remove_dot_segments of RFC 3986 section 5.2.4."""
    out: list[str] = []
    while path:
        path = _dot_step(path, out)
    return "".join(out)


def _join(
    scheme: str | None, authority: str | None, path: str, query: str | None, fragment: str | None
) -> str:
    """Return the URI that RFC 3986 section 5.3 recomposes from its five parts."""
    text = f"{scheme}:" if scheme is not None else ""
    text += f"//{authority}" if authority is not None else ""
    text += path
    text += f"?{query}" if query is not None else ""
    return text + (f"#{fragment}" if fragment is not None else "")


def _resolve(base: str, reference: str) -> str:
    """Return ``reference`` resolved against ``base`` by RFC 3986 section 5.2.2, strict."""
    scheme, authority, path, query, fragment = _split(reference)
    b_scheme, b_authority, b_path, b_query, _fragment = _split(base)
    if scheme is not None:
        return _join(scheme, authority, _remove_dots(path), query, fragment)
    if authority is not None:
        return _join(b_scheme, authority, _remove_dots(path), query, fragment)
    if not path:
        return _join(b_scheme, b_authority, b_path, b_query if query is None else query, fragment)
    if not path.startswith("/"):
        # Section 5.2.3: merge the reference path with the base path.
        path = (
            "/" if b_authority is not None and not b_path else b_path[: b_path.rfind("/") + 1]
        ) + path
    return _join(b_scheme, b_authority, _remove_dots(path), query, fragment)


segments = st.one_of(st.sampled_from((".", "..")), st.text(_PCHARS, min_size=1, max_size=4))
relative_paths = st.builds(
    lambda root, parts: root + "/".join(parts),
    st.sampled_from(("", "/")),
    st.lists(segments, max_size=4),
).filter(lambda path: not path.endswith(";"))
suffixes = st.one_of(st.none(), st.text(_PCHARS + "/?:", min_size=1, max_size=5))
references = st.one_of(
    st.sampled_from(("", "#f", "?q", ".", "..", "/")),
    st.builds(
        lambda path, query, fragment: _join(None, None, path, query, fragment),
        relative_paths,
        suffixes,
        suffixes,
    ),
)
bases = st.builds(
    lambda folder, name, query: _join(None, None, folder + name, query, None),
    strategies.folders,
    st.text(strategies.UNRESERVED, max_size=6).filter(lambda name: name not in {".", ".."}),
    suffixes,
)


@given(bases, references)
def test_a_relative_href_resolves_as_rfc_3986_says(base: str, reference: str) -> None:
    # RFC 3986 section 5.2.2, "Transform References", in its strict form.
    assert hrefs.absolute_href(reference, base) == _resolve(base, reference)


s3_websites = st.builds(
    "http://{}.s3-website-{}.amazonaws.com/{}".format,
    st.from_regex(r"[a-z0-9]([a-z0-9-]|\.[a-z0-9]){1,20}", fullmatch=True),
    st.sampled_from(("us-east-1", "eu-west-3")),
    strategies.paths(),
)
absolutes = st.one_of(
    st.builds(operator.add, strategies.folders, relative_paths),
    s3_websites,
)


@given(absolutes, bases, bases)
def test_resolving_an_absolute_href_is_idempotent(href: str, base: str, other: str) -> None:
    resolved = hrefs.absolute_href(href, base)
    assert hrefs.absolute_href(resolved, other) == resolved
    assert hrefs.absolute_href(href, other) == resolved
    if not (urlsplit(href).hostname or "").endswith(".amazonaws.com"):
        # urljoin returns an absolute href as it is, with its dot segments.
        assert resolved == href


# AWS general purpose bucket names: 3 to 63 characters, lower case letters,
# digits, dots, and hyphens, starting and ending with a letter or a digit,
# with no two dots in a row.
s3_buckets = st.from_regex(r"[a-z0-9]([a-z0-9-]|\.[a-z0-9]){1,30}[a-z0-9]", fullmatch=True)
keys = strategies.paths(1)


@given(st.sampled_from(("s3", "gs")), s3_buckets, keys, bases)
def test_a_bucket_uri_round_trips_through_https(
    scheme: str, bucket: str, key: str, base: str
) -> None:
    # formats.md, Partitioned Collections: "Clients assume an `s3://` URL refers to Amazon S3."
    uri = f"{scheme}://{bucket}/{key}"
    https = hrefs.asset_href(uri, base)
    assert https.startswith("https://")
    assert hrefs.bucket_url(https) == uri


@given(st.text())
def test_vsicurl_keeps_the_url(url: str) -> None:
    query = hrefs.vsicurl_path(url).split("?", 1)[1]
    assert parse_qs(query, keep_blank_values=True, strict_parsing=True)["url"] == [url]
    assert hrefs.plain_vsicurl_path(url).removeprefix("/vsicurl/") == url


@given(strategies.folders, keys, st.lists(st.sampled_from(("*", "**", "[0-9]", "x*y")), max_size=2))
def test_a_glob_root_holds_no_glob(folder: str, path: str, globs: list[str]) -> None:
    href = folder + path + "".join(f"/{glob}" for glob in globs)
    root = hrefs.glob_root(href)
    assert not hrefs.is_glob(root)
    assert href.startswith(root)
    assert hrefs.is_glob(href) == bool(globs)


@given(st.text())
def test_browser_url_refuses_only_what_is_not_http(href: str) -> None:
    if hrefs.is_http_url(href):
        assert hrefs.browser_url(href).startswith(hrefs.BROWSER_URL)
    else:
        with pytest.raises(ValueError, match="Not an http"):
            hrefs.browser_url(href)
