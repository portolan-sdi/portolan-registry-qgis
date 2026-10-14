#!/usr/bin/env python3
"""Run the plugin's core read path on every registered catalog and report what fails.

Each collection gets the checks the GUI makes, without QGIS. Writes Markdown and JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from portolan_registry_qgis.core import parquet_query, registry, style  # noqa: E402
from portolan_registry_qgis.core.download import files_of  # noqa: E402
from portolan_registry_qgis.core.loadable import pick_targets  # noqa: E402
from portolan_registry_qgis.core.mirror import mirror_href  # noqa: E402
from portolan_registry_qgis.core.stac import Document, read_document  # noqa: E402

TIMEOUT = 30
USER_AGENT = "portolan-registry-qgis-sweep"
GLOB = re.compile(r"[*?\[]")
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8")
PMTILES_MAGIC = b"PMTiles"


@dataclass(frozen=True)
class Result:
    """One check on one document."""

    catalog: str
    document: str
    check: str
    ok: bool
    detail: str = ""


def encoded(url: str) -> str:
    """Percent-encode spaces and non-ASCII characters, as QUrl does in tolerant mode."""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def open_url(request: urllib.request.Request) -> Any:
    """Open a request, and retry once after a timeout."""
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT)
    except (TimeoutError, urllib.error.URLError) as error:
        if isinstance(error, urllib.error.HTTPError):
            raise
        time.sleep(2)
        return urllib.request.urlopen(request, timeout=TIMEOUT)


def fetch(url: str, byte_range: str | None = None) -> tuple[bytes, str, dict[str, str]]:
    """Return the body, the final URL after redirects, and the headers of a GET."""
    request = urllib.request.Request(encoded(url), headers={"User-Agent": USER_AGENT})
    if byte_range:
        request.add_header("Range", f"bytes={byte_range}")
    with open_url(request) as response:
        return response.read(), response.geturl(), dict(response.headers)


def head(url: str) -> dict[str, str]:
    """Return the headers of a HEAD request."""
    request = urllib.request.Request(
        encoded(url), method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    with open_url(request) as response:
        return dict(response.headers)


def is_glob(href: str) -> bool:
    """Return whether the path of ``href`` holds a glob character."""
    return bool(GLOB.search(urllib.parse.urlsplit(href).path))


def fetch_document(url: str) -> tuple[Document, dict[str, Any], str]:
    """Fetch and read a STAC document as the plugin does, keyed on the requested URL."""
    body, final, _headers = fetch(url)
    raw = json.loads(body)
    return read_document(raw, url), raw, final


class Sweep:
    """Collect results for one catalog."""

    def __init__(self, catalog: str, max_documents: int, con: Any) -> None:
        self.catalog = catalog
        self.max_documents = max_documents
        self.con = con
        self.results: list[Result] = []

    def record(self, document: str, check: str, run: Callable[[], str]) -> None:
        """Run one check. An exception is a failure, and its text is the detail."""
        try:
            detail = run()
        except Exception as error:  # noqa: BLE001 - the report shows every failure
            text = f"{type(error).__name__}: {error}".splitlines()[0][:300]
            self.results.append(Result(self.catalog, document, check, False, text))
            return
        self.results.append(Result(self.catalog, document, check, True, detail))

    def flag(self, document: str, check: str, message: str) -> None:
        """Record a failure found without running a request."""
        self.results.append(Result(self.catalog, document, check, False, message))

    def walk(self, root: str) -> Iterator[tuple[Document, dict[str, Any]]]:
        """Yield each catalog and collection, and the first item of each collection."""
        queue, seen, reads = [root], {root}, 0
        while queue and reads < self.max_documents:
            url = queue.pop(0)
            reads += 1
            try:
                document, raw, final = fetch_document(url)
            except Exception as error:  # noqa: BLE001
                self.flag(url, "read", f"{type(error).__name__}: {error}"[:300])
                continue
            if final != encoded(url):
                self.flag(url, "redirect", f"fetched from {final}; hrefs resolve against {url}")
            yield document, raw
            items = [node for node in document.children if node.kind == "item"][:1]
            others = [node for node in document.children if node.kind != "item"]
            for node in [*others, *items]:
                if node.href not in seen:
                    seen.add(node.href)
                    queue.append(node.href)

    def check(self, document: Document, raw: dict[str, Any]) -> None:
        """Run every check that applies to one document."""
        self.raw_hrefs(document, raw)
        if document.kind == "catalog":
            return
        self.targets(document)
        self.styles(document)
        self.thumbnail(document)
        self.downloads(document)
        mirror = mirror_href(document)
        if mirror:
            self.record(document.href, "mirror", lambda: self.parquet(mirror))

    def raw_hrefs(self, document: Document, raw: dict[str, Any]) -> None:
        """Flag raw hrefs and spec fields the plugin drops or misreads."""
        glob = raw.get("partition:glob")
        if glob:
            self.flag(document.href, "partition:glob", f"plugin ignores partition:glob {glob!r}")
        kept = {asset.key for asset in document.assets}
        for key, asset in (raw.get("assets") or {}).items():
            href = asset.get("href", "") if isinstance(asset, dict) else ""
            if key not in kept:
                self.flag(document.href, "asset dropped", f"{key}: {href}")
            elif is_glob(href):
                self.flag(document.href, "glob href", f"{key}: {href}")
        if document.kind == "collection" and not document.loadable() and not document.children:
            self.flag(document.href, "no data", "nothing to load and no items")

    def targets(self, document: Document) -> None:
        """Probe the files behind "View on map" and "Load full data"."""
        view, load = pick_targets(document.loadable(), duckdb_ok=True)
        if view is not None:
            self.record(
                document.href, f"view {view.format}", lambda: self.probe(view.href, view.format)
            )
        if load is None:
            return
        if load.format == "parquet":
            self.record(document.href, "load parquet", lambda: self.parquet(load.href))
        else:
            self.record(
                document.href, f"load {load.format}", lambda: self.probe(load.href, load.format)
            )

    def probe(self, url: str, kind: str) -> str:
        """Check range support, Content-Length, and the PMTiles magic of one file."""
        headers = head(url)
        if "Content-Length" not in headers:
            raise ValueError("HEAD returned no Content-Length")
        body, _final, got = fetch(url, "0-126")
        if len(body) > 127:
            raise ValueError("server ignored the Range header")
        if kind == "pmtiles" and not body.startswith(PMTILES_MAGIC):
            raise ValueError(f"not a PMTiles archive (starts {body[:8]!r})")
        return f"{headers['Content-Length']} bytes, {got.get('Content-Type', '?')}"

    def parquet(self, url: str) -> str:
        """Read the schema, the geo metadata, and the estimate, as "Load full data" does."""
        plan = parquet_query.read_plan(self.con, url)
        count = parquet_query.estimate(self.con, url, plan)
        return f"geometry={plan.geometry!r} estimate={count}"

    def styles(self, document: Document) -> None:
        """Check the default style and that its PMTiles sources resolve to archives."""
        styles, archives = document.styles, style.archive_urls(document)
        if archives and not styles and document.kind == "collection":
            self.flag(document.href, "style", "PMTiles with no style asset")
        if len(styles) > 1 and document.default_style is None:
            self.flag(document.href, "style", f"{len(styles)} styles and none has role default")
        chosen = document.default_style or (styles[0] if styles else None)
        if chosen is not None:
            fallback = archives[0] if len(archives) == 1 else None
            self.record(document.href, "style", lambda: self.style(chosen.href, archives, fallback))

    def style(self, url: str, archives: list[str], fallback: str | None) -> str:
        """Fetch a style, match its sources to archives, and fetch its sprite."""
        body, _final, _headers = fetch(url)
        parsed = style.parse_style(body)
        sources = style.pmtiles_sources(parsed, url, fallback)
        if not sources:
            raise ValueError("no PMTiles source in the style")
        stray = sorted(set(sources.values()) - set(archives))
        for archive in stray:
            self.probe(archive, "pmtiles")
        sprite = style.sprite_root(parsed, url)
        if sprite:
            fetch(f"{sprite}.json")
        return f"{len(sources)} sources, {len(stray)} outside the collection links"

    def thumbnail(self, document: Document) -> None:
        """Fetch the thumbnail and check that it is an image."""
        thumb = document.thumbnail
        if thumb is None:
            if document.kind == "collection":
                self.flag(document.href, "thumbnail", "no thumbnail asset")
            return

        def run() -> str:
            body, _final, _headers = fetch(thumb.href)
            if not body.startswith(IMAGE_MAGIC):
                raise ValueError(f"not an image (starts {body[:8]!r})")
            return f"{len(body)} bytes"

        self.record(document.href, "thumbnail", run)

    def downloads(self, document: Document) -> None:
        """Flag download paths with a glob or no file name."""
        for planned in files_of(document, document.href):
            name = planned.path.rsplit("/", 1)[-1]
            if is_glob(planned.url) or not name or name == "index":
                self.flag(document.href, "download path", f"{planned.url} -> {planned.path}")


def sweep_catalog(entry: registry.CatalogEntry, max_documents: int, con: Any) -> list[Result]:
    """Walk one catalog and return its results."""
    sweep = Sweep(entry.id, max_documents, con)
    for document, raw in sweep.walk(entry.url):
        sweep.check(document, raw)
    return sweep.results


def markdown(results: list[Result]) -> str:
    """Return the failures as a Markdown report with a count per check."""
    failures = [result for result in results if not result.ok]
    counts = Counter(result.check for result in failures)
    lines = [
        f"# Registry sweep: {len(failures)} failures in {len(results)} checks",
        "",
        "| Check | Failures |",
        "|---|---|",
        *(f"| {check} | {count} |" for check, count in counts.most_common()),
        "",
        "| Catalog | Document | Check | Detail |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {r.catalog} | {r.document} | {r.check} | {r.detail.replace('|', '/')} |"
        for r in sorted(failures, key=lambda r: (r.check, r.catalog, r.document))
    ]
    return "\n".join(lines) + "\n"


def registry_entries(only: list[str] | None) -> list[registry.CatalogEntry]:
    """Return the registered catalogs, or those whose ids are in ``only``."""
    body, _final, _headers = fetch(registry.REGISTRY_URL)
    entries = registry.parse_registry(json.loads(body))
    return [entry for entry in entries if entry.id in only] if only else entries


def write_report(out: Path, results: list[Result]) -> int:
    """Write ``sweep.json`` and ``sweep.md`` to ``out``. Return 1 when a check failed."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "sweep.json").write_text(json.dumps([asdict(r) for r in results], indent=1))
    (out / "sweep.md").write_text(markdown(results))
    return 1 if any(not r.ok for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    """Sweep the registry and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("sweep"))
    parser.add_argument("--only", nargs="*", default=None, help="catalog ids to sweep")
    parser.add_argument("--max-documents", type=int, default=300)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    entries = registry_entries(args.only)
    con = parquet_query.connection()
    lock = threading.Lock()
    results: list[Result] = []

    def run(entry: registry.CatalogEntry) -> None:
        found = sweep_catalog(entry, args.max_documents, con)
        with lock:
            results.extend(found)
        print(f"{entry.id}: {sum(not r.ok for r in found)} failures", file=sys.stderr)

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(run, entries))
    return write_report(args.out, results)


if __name__ == "__main__":
    sys.exit(main())
