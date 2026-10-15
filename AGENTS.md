<!-- ops-sync:begin — synced from portolan-sdi/portolan-ops. Edit there, not here. -->
# Portolan agent norms

All agents MUST read this document before starting work, with no exceptions, ever. (Claude Code does not read `AGENTS.md` but imports this file via a one-line `CLAUDE.md`.) Most of this content is synced downstream from `portolan-ops`. Put repo-specific instructions in `AGENTS.md`, never in `CLAUDE.md`, which the sync overwrites.

## Ground rules

Agents MUST read the complete contents of [norms](https://github.com/portolan-sdi/portolan-ops/blob/main/norms) and [policies](https://github.com/portolan-sdi/portolan-ops/blob/main/policies/) before starting work, with no exceptions, ever.

The [portolan-spec](https://github.com/portolan-sdi/portolan-spec) repo is the absolute ground truth for the Portolan spec. All other tools are downstream of it.

Before documenting any command, flag, or API, verify that it exists.

Use conventional commits always.

All tickets and PRs MUST follow the [templates](https://github.com/portolan-sdi/portolan-ops/blob/main/templates/).

## Org-wide facts

The canonical homepage is https://www.portolan-sdi.org/. Canonical URLs live in [copy/urls.md](https://github.com/portolan-sdi/portolan-ops/blob/main/copy/urls.md). Do not hardcode variants.

Community discussion happens in the [Portolan Google Group](https://groups.google.com/g/portolan) and the [Portolan channel](https://cloudnativegeo.slack.com/archives/C0A1JBH9529) in Cloud-Native Geo Slack. Planning happens in [org-level GitHub projects](https://github.com/orgs/portolan-sdi/projects/1).

Files between `ops-sync` markers are synced from [portolan-ops](https://github.com/portolan-sdi/portolan-ops). They are overwritten on every sync run. To change one, edit it in portolan-ops.

One canonical home per fact. If a value like a color, URL, or policy line exists in portolan-ops, link to it rather than copying it.
<!-- ops-sync:end -->

# Repo-specific instructions

<!-- Add instructions for this repo below. The block above is overwritten by
     sync. This file is the only home for repo-specific agent rules: CLAUDE.md
     carries the import and nothing else. -->

`portolan_registry_qgis/core` holds the STAC, registry, PMTiles, GeoParquet, and download logic. It must not import `qgis`, `PyQt`, or `osgeo`. import-linter enforces this, because the uv test matrix imports the core without QGIS.

QGIS code goes in `qgis_io` (network, tiles, layers, downloads) or `gui` (widgets). Use fully scoped Qt enums such as `Qt.ItemDataRole.UserRole`, so the plugin runs on Qt5 and Qt6.

QGIS does not include DuckDB, so the plugin imports it inside functions only. Keep every `import duckdb` inside a function.

Run `tests/in_qgis` before you push a change to `qgis_io`, `gui`, or `plugin.py`. `docs/development.md` gives the commands. The uv suite cannot catch those failures.

Keep each docstring to one summary line, and 5 lines at most. State what the code does. Put the reason for a change in the commit message, not in a docstring or a comment. `scripts/docstring_check.py` enforces this.
