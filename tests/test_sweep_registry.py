from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler

import pytest

from portolan_registry_qgis.core import parquet_query, registry
from scripts import sweep_registry
from tests.loopback import base_url, serve


@pytest.mark.parametrize(
    ("catalog_exists", "catalog_warnings", "status"),
    [(True, True, 0), (False, True, 0), (False, False, 1)],
)
def test_sweep_reports_downstream_catalog_findings(
    tmp_path, monkeypatch, catalog_exists, catalog_warnings, status
):
    export = {
        "type": "Catalog",
        "links": [{"rel": "child", "href": "catalog.json", "portolan_registry:id": "sample"}],
    }
    (tmp_path / "export.json").write_text(json.dumps(export))
    if catalog_exists:
        (tmp_path / "catalog.json").write_text(json.dumps({"type": "Catalog", "links": []}))
    monkeypatch.setattr(parquet_query, "connection", lambda: None)
    out = tmp_path / "report"
    with serve(partial(SimpleHTTPRequestHandler, directory=tmp_path)) as server:
        monkeypatch.setattr(registry, "REGISTRY_URL", f"{base_url(server)}/export.json")
        args = ["--out", str(out), "--only", "sample"]
        if catalog_warnings:
            args.insert(0, "--catalog-warnings")
        assert sweep_registry.main(args) == status
    failures = [row for row in json.loads((out / "sweep.json").read_text()) if not row["ok"]]
    if catalog_exists:
        assert failures == []
    else:
        assert [row["check"] for row in failures] == ["read"]
        assert "HTTP Error 404" in (out / "sweep.md").read_text()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({}, "invalid catalog list"),
        ({"type": "Catalog", "links": []}, "no readable catalog links"),
    ],
)
def test_unreadable_registry_export_fails_with_catalog_warnings(
    tmp_path, monkeypatch, document, message
):
    (tmp_path / "export.json").write_text(json.dumps(document))
    with serve(partial(SimpleHTTPRequestHandler, directory=tmp_path)) as server:
        monkeypatch.setattr(registry, "REGISTRY_URL", f"{base_url(server)}/export.json")
        with pytest.raises(registry.RegistryError, match=message):
            sweep_registry.main(["--catalog-warnings", "--out", str(tmp_path / "report")])


def test_unexpected_check_error_propagates():
    def crash():
        raise RuntimeError

    with pytest.raises(RuntimeError):
        sweep_registry.Sweep("sample", 1, None).record("catalog.json", "read", crash)
