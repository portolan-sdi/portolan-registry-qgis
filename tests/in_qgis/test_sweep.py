from __future__ import annotations

import subprocess
import sys

import pytest

from scripts import sweep_qgis
from scripts.sweep_registry import registry


def test_parallel_sweep_propagates_worker_exit(tmp_path, monkeypatch):
    worker = tmp_path / "worker.py"
    worker.write_text("raise SystemExit(7)\n")
    monkeypatch.setattr(sweep_qgis, "__file__", str(worker))
    entries = registry.parse_registry(
        {
            "type": "Catalog",
            "links": [{"rel": "child", "href": "https://example.test/catalog.json"}],
        }
    )

    with pytest.raises(subprocess.CalledProcessError) as error:
        sweep_qgis.spawn(entries, tmp_path / "catalogs", jobs=2, max_documents=1)

    assert error.value.returncode == 7
    assert error.value.cmd[0] == sys.executable
