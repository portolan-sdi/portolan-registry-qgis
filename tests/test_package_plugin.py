"""QGIS plugin archive tests."""

from __future__ import annotations

import runpy
import sys
import zipfile
from typing import TYPE_CHECKING

from scripts.package_plugin import build_archive, vendor_portolan

if TYPE_CHECKING:
    from pathlib import Path


def test_package_has_qgis_root_and_vendored_portolan(tmp_path: Path) -> None:
    output = tmp_path / "portolan-registry-qgis.zip"

    build_archive(output)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())

    assert "portolan_registry_qgis/__init__.py" in names
    assert "portolan_registry_qgis/metadata.txt" in names
    assert "portolan_registry_qgis/portolan/__init__.py" in names
    assert {name.split("/", 1)[0] for name in names} == {"portolan_registry_qgis"}
    assert not any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names)


def test_vendor_command_prepares_plugin_for_qgis_plugin_ci(tmp_path: Path) -> None:
    plugin_root = tmp_path / "portolan_registry_qgis"
    plugin_root.mkdir()

    vendor_portolan(plugin_root)

    assert (plugin_root / "portolan" / "__init__.py").is_file()


def test_packaged_plugin_activates_its_bundled_dependency(tmp_path: Path) -> None:
    output = build_archive(tmp_path / "plugin.zip")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(extracted)
    plugin_root = extracted / "portolan_registry_qgis"
    plugin_path = str(plugin_root)

    assert plugin_path not in sys.path
    try:
        runpy.run_path(str(plugin_root / "__init__.py"))
        assert sys.path[0] == plugin_path
    finally:
        if plugin_path in sys.path:
            sys.path.remove(plugin_path)
