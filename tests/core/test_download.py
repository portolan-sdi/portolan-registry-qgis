from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from portolan_registry_qgis.core.download import local_path, plan

FIXTURES = Path(__file__).parent.parent / "fixtures" / "anncsu"
ROOT = "https://pub-1e760dc850cb4a5aa5f8afb77713f8cd.r2.dev/catalog.json"
BASE = ROOT.rsplit("/", 1)[0]


@pytest.mark.parametrize(
    ("url", "path"),
    [
        (f"{BASE}/catalog.json", "catalog.json"),
        (f"{BASE}/indirizzi/collection.json", "indirizzi/collection.json"),
        (f"{BASE}/a%20b/c.parquet", "a_b/c.parquet"),
        ("https://other.test/x/y.tif", "_external/other.test/x/y.tif"),
        # Traversal and odd characters cannot escape the target folder.
        (f"{BASE}/../../etc/passwd", "etc/passwd"),
        ("https://other.test/..%2F..%2Fsecret", "_external/other.test/secret"),
        ("https://other.test/", "_external/other.test"),
        ("https://other.test", "_external/other.test"),
        # Same host, different scheme: not under the root.
        (BASE.replace("https", "http") + "/a.json", "_external/" + BASE[8:] + "/a.json"),
    ],
)
def test_local_path(url, path):
    assert local_path(url, ROOT) == path


def fake_fetch(documents):
    def fetch(url):
        if url not in documents:
            raise OSError(f"404 {url}")
        return documents[url]

    return fetch


def anncsu_documents():
    catalog = json.loads((FIXTURES / "catalog.json").read_text(encoding="utf-8"))
    collection = json.loads((FIXTURES / "indirizzi/collection.json").read_text(encoding="utf-8"))
    return {ROOT: catalog, f"{BASE}/indirizzi/collection.json": collection}


def test_plan_walks_catalog_and_records_failures():
    result = plan(fake_fetch(anncsu_documents()), ROOT)
    paths = [f.path for f in result.files]
    assert paths[:2] == ["catalog.json", "indirizzi/collection.json"]
    assert "anncsu-indirizzi.parquet" in paths
    assert "anncsu-indirizzi.pmtiles" in paths
    assert "indirizzi/styles/indirizzi.json" in paths
    # The PMTiles link and the "visual" asset name the same file once.
    assert paths.count("anncsu-indirizzi.pmtiles") == 1
    assert [url for url, _ in result.failures] == [
        f"{BASE}/indirizzi-h3/collection.json",
        f"{BASE}/rilasci/collection.json",
    ]
    assert result.known_bytes >= 1059267903 + 333155352
    assert result.unknown_sizes == 2
    assert not result.truncated


def test_plan_of_one_collection_keeps_catalog_layout():
    href = f"{BASE}/indirizzi/collection.json"
    result = plan(fake_fetch(anncsu_documents()), href, root=ROOT)
    assert result.files[0].path == "indirizzi/collection.json"
    data = next(f for f in result.files if f.path == "anncsu-indirizzi.parquet")
    assert data.size == 1059267903
    assert data.checksum.startswith("1220")
    assert result.failures == []


def test_plan_stops_at_the_limit_and_on_cancel():
    documents = anncsu_documents()
    assert plan(fake_fetch(documents), ROOT, max_documents=1).truncated
    cancelled = plan(fake_fetch(documents), ROOT, cancelled=lambda: True)
    assert cancelled.files == []


def test_plan_skips_cycles():
    a = "https://x.test/a/catalog.json"
    documents = {
        a: {"type": "Catalog", "id": "a", "links": [{"rel": "child", "href": "./b/catalog.json"}]},
        "https://x.test/a/b/catalog.json": {
            "type": "Catalog",
            "id": "b",
            "links": [{"rel": "child", "href": "../catalog.json"}],
        },
    }
    result = plan(fake_fetch(documents), a)
    assert [f.path for f in result.files] == ["catalog.json", "b/catalog.json"]


def fan_out(count):
    """A catalog with ``count`` child catalogs, keyed by URL."""
    root = "https://x.test/catalog.json"
    children = [f"https://x.test/c{i}/catalog.json" for i in range(count)]
    documents = {
        root: {
            "type": "Catalog",
            "id": "root",
            "links": [{"rel": "child", "href": f"./c{i}/catalog.json"} for i in range(count)],
        }
    }
    for i, href in enumerate(children):
        documents[href] = {"type": "Catalog", "id": f"c{i}", "links": []}
    return root, children, documents


def test_walk_reads_a_level_in_parallel():
    # Every child read waits until 8 reads are in flight. A walk that reads
    # one document at a time breaks the barrier and fails.
    root, children, documents = fan_out(16)
    barrier = threading.Barrier(8, timeout=10)
    lock = threading.Lock()
    active = peak = 0

    def fetch(url):
        nonlocal active, peak
        if url == root:
            return documents[url]
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait()
        finally:
            with lock:
                active -= 1
        return documents[url]

    result = plan(fetch, root)
    assert result.failures == []
    assert peak == 8
    assert [f.url for f in result.files] == [root, *children]


def test_walk_keeps_link_order_when_reads_finish_out_of_order():
    root, children, documents = fan_out(12)
    failing = children[3]
    del documents[failing]

    def fetch(url):
        # Later links answer first.
        if url != root:
            time.sleep(0.002 * (12 - children.index(url)))
        if url not in documents:
            raise OSError(f"404 {url}")
        return documents[url]

    result = plan(fetch, root, workers=4)
    assert [f.url for f in result.files] == [root, *(c for c in children if c != failing)]
    assert result.failures == [(failing, f"404 {failing}")]


def test_walk_respects_the_limit_inside_a_level():
    root, _children, documents = fan_out(20)
    reads = []

    def fetch(url):
        reads.append(url)
        return documents[url]

    result = plan(fetch, root, max_documents=5)
    assert len(reads) == 5
    assert len(result.files) == 5
    assert result.truncated


def test_walk_stops_reading_on_cancel():
    root, _children, documents = fan_out(40)
    reads = []
    stop = threading.Event()

    def fetch(url):
        reads.append(url)
        if len(reads) >= 3:
            stop.set()
        return documents[url]

    plan(fetch, root, cancelled=stop.is_set, workers=2)
    # The reads already started may finish. No new read starts.
    assert len(reads) <= 4


def test_walk_uses_a_given_executor_and_leaves_it_open():
    root, children, documents = fan_out(6)
    threads = set()

    def fetch(url):
        threads.add(threading.current_thread().name)
        return documents[url]

    with ThreadPoolExecutor(2, thread_name_prefix="caller") as executor:
        result = plan(fetch, root, executor=executor)
        assert executor.submit(lambda: "open").result() == "open"
    assert [f.url for f in result.files] == [root, *children]
    assert threads and all(name.startswith("caller") for name in threads)
