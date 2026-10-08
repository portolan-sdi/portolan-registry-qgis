#!/usr/bin/env python3
"""Build an installable QGIS plugin ZIP with portolan-python included."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import tempfile
import zipfile
from pathlib import Path

PLUGIN_NAME = "portolan_registry_qgis"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ignore_generated(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def _portolan_package() -> Path:
    spec = importlib.util.find_spec("portolan")
    locations = spec.submodule_search_locations if spec is not None else None
    if not locations:
        raise RuntimeError("portolan-python is not installed")
    return Path(next(iter(locations)))


def vendor_portolan(target: Path) -> None:
    """Copy portolan-python into an existing QGIS plugin directory."""
    shutil.copytree(
        _portolan_package(),
        target / "portolan",
        dirs_exist_ok=True,
        ignore=_ignore_generated,
    )


def prepare_plugin(target: Path) -> None:
    """Copy the QGIS plugin and its Portolan API dependency into ``target``."""
    shutil.copytree(
        PROJECT_ROOT / PLUGIN_NAME,
        target,
        dirs_exist_ok=True,
        ignore=_ignore_generated,
    )
    vendor_portolan(target)


def build_archive(output: Path) -> Path:
    """Create a QGIS-installable archive at ``output``."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portolan-registry-qgis-") as temporary:
        plugin_root = Path(temporary) / PLUGIN_NAME
        prepare_plugin(plugin_root)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(plugin_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(plugin_root.parent))
    return output


def main() -> int:
    """Run the plugin packager command."""
    parser = argparse.ArgumentParser(description=__doc__)
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument(
        "--output",
        type=Path,
        help="Write an installable QGIS ZIP to this path.",
    )
    outputs.add_argument(
        "--vendor-into",
        type=Path,
        help="Copy portolan-python into an existing plugin directory.",
    )
    args = parser.parse_args()
    if args.vendor_into is not None:
        vendor_portolan(args.vendor_into)
        print(args.vendor_into / "portolan")
    else:
        output = args.output or PROJECT_ROOT / "dist" / "portolan-registry-qgis.zip"
        print(build_archive(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
