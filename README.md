# portolan-registry-qgis

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

A QGIS plugin for the [Portolan registry](https://github.com/portolan-sdi/portolan-registry). Browse its catalogs, then add their data to the map or download it. PMTiles draw with each collection's MapLibre style, and GeoParquet loads through DuckDB.

## Quick start

```bash
git clone https://github.com/portolan-sdi/portolan-registry-qgis
PLUGINS=~/.local/share/QGIS/QGIS3/profiles/default/python/plugins
ln -s "$PWD/portolan-registry-qgis/portolan_registry_qgis" \
  "$PLUGINS/portolan_registry_qgis"
```

Restart QGIS, enable **Portolan Registry** in the plugin manager, and open it from the **Web** menu. GeoParquet needs DuckDB 1.5.0 or newer in QGIS's Python.

## Documentation

See the [documentation](https://portolan-sdi.github.io/portolan-registry-qgis/) for installation, including Flatpak, usage, and development. The plugin and its documentation are also in [Spanish](https://portolan-sdi.github.io/portolan-registry-qgis/es/) and [French](https://portolan-sdi.github.io/portolan-registry-qgis/fr/).

## Contributing

See the org [contributing guide](https://github.com/portolan-sdi/.github/blob/main/CONTRIBUTING.md), including the AI policy. This repo implements the [Portolan spec](https://github.com/portolan-sdi/portolan-spec).

## License

[Apache-2.0](LICENSE). The STAC tree and link logic comes from [GeoLibre](https://github.com/opengeos/GeoLibre) (MIT).
