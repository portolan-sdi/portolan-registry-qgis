<!-- ops-sync:begin — synced from portolan-sdi/portolan-ops. Edit there, not here. -->
# Portolan agent norms

These rules apply to AI agents working in any portolan-sdi repo. Every downstream repo repeats this text verbatim as a synced block at the top of its own `AGENTS.md`. Repo-specific instructions live below the block and override the canonical rules in that repo only.

Claude Code does not read `AGENTS.md`. Each repo has a one-line `CLAUDE.md` that imports it instead. Put repo-specific instructions in `AGENTS.md`, never in `CLAUDE.md`, which the sync overwrites.

## Ground rules

The [portolan-spec](https://github.com/portolan-sdi/portolan-spec) repo is ground truth for the Portolan specification. The CLI, `rashid`, the registry, and every other tool implement the specification. They are downstream of it. Never describe the CLI as the source of truth. Propose spec changes in portolan-spec.

Before documenting any command, flag, or API, verify it exists in the released tool. A fabricated example persists beyond the session that wrote it.

Every repo uses Apache-2.0 except portolan-browser and portolan-nl-demo, which are ISC forks. See [norms/repos.md](https://github.com/portolan-sdi/portolan-ops/blob/main/norms/repos.md) for the record. Never introduce code under another license without a human decision recorded there.

Never bypass pre-commit hooks or CI gates. Green means green.

Write commits in conventional form. Squash-merge makes the pull request title become the commit message.

## Pull requests and issues

Write every issue and pull request in two layers. The human layer first states what is wrong or missing. It explains why the problem matters and what should happen instead. Someone who did not follow your investigation should understand it in about a minute. The agent layer then provides evidence and implementation detail. It also records constraints, edge cases, and verification.

There is no word limit. A 700-word issue is good when its first 150 words make the outcome obvious. A 150-word issue is bad when it compresses the meaning into prose the reader has to unpack. Optimize for fast comprehension, not for short tickets.

Write them in Simplified Technical English (ASD-STE100). The rules are an output style, `.claude/output-styles/simplified-technical-english.md`, which every repo receives. A hook prints it at session start. Sentences run under 20 words and express one idea. Use the active voice, and use only the infinitive, the imperative, and simple tenses. Use a verb rather than a noun made from a verb. Keep the technical content exactly as precise as it was, and simplify only the language around it. Describe the design as it stands now rather than the approaches you discarded.

The structural contract CI enforces on a pull request:

- The sections `## What changed`, `## Why`, and `## Verification` exist and are not empty.
- The prose references the issue the change resolves, as `#N` or its URL.
- Verification pastes the command you ran and its output in a fenced block under `## Verification`. It identifies the data it read, as a URL or catalog path.
- A change that alters no behavior ticks the waiver checkbox instead. Keep its wording intact because the check matches the phrase "does not alter behavior".

Good evidence shows the fix works against real data. Proving a command exits zero is not enough. Take the failing command from the issue, run it against the same catalog, and show it now succeeds. A wall of pytest output does not count.

Issues follow the same shape. A bug report shows the failure and identifies the data. Write a feature request to show what the current tool cannot do, or what the workaround costs. A task states the outcome and the command that proves it is done.

Each repo uses the org issue template. The language itself is checked before a body is ever filed: `.claude/hooks/writing_check.py` runs on `gh issue create` and `gh pr create`, and reports the specific problems it found. Run `writing_check.py --print-rules` to read the rules. When it is wrong about a line, say so in the body with `<!-- ste-ok: RULE_ID why this is correct -->`. Dependabot is exempt from the CI check.

That check matches words and punctuation, but cannot assess tone or padding. It also cannot assess prose that argues for its own value. Passing proves nothing about how the body reads. Read what you wrote before you file it. Cut sentences that only make the change sound good.

## Documentation

Agents writing or restructuring documentation follow two exemplars named in [norms/docs.md](https://github.com/portolan-sdi/portolan-ops/blob/main/norms/docs.md). [obstore](https://github.com/developmentseed/obstore) demonstrates a concise, human-readable README that delegates to good docs elsewhere. [scaffold-docs-skill](https://github.com/dbreunig/scaffold-docs-skill) shows how to build docs that have a clear human-facing surface, maintain examples via tests so they never drift, and auto-generate API docs instead of duplicating them. Both make documentation easy to maintain and easy to update. Draft top-down with human review between layers. Do not draft a README from a generic template or from memory.

These rules apply to every docs change. Use sentence-case headings without emoji. Use absolute dates like "in July 2026", never "recently". Run every command example against the released tool before you publish it.

## Voice and messaging

Every written artifact follows the prose rules in [norms/prose.md](https://github.com/portolan-sdi/portolan-ops/blob/main/norms/prose.md). Vale checks Markdown and website copy. The writing hook checks the prose in issues and pull requests. Apply the rules while drafting, not as cleanup.

Before drafting substantial public copy like a README, a docs page, or an announcement, fetch and read [norms/prose.md](https://github.com/portolan-sdi/portolan-ops/blob/main/norms/prose.md) and [copy/messaging.md](https://github.com/portolan-sdi/portolan-ops/blob/main/copy/messaging.md) in full. If you cannot fetch them, say so and stop. Write from the actual files, not from memory.

How Portolan is described comes from [copy/messaging.md](https://github.com/portolan-sdi/portolan-ops/blob/main/copy/messaging.md) alone.

## Org-wide facts

The canonical homepage is https://www.portolan-sdi.org/. Canonical URLs live in [copy/urls.md](https://github.com/portolan-sdi/portolan-ops/blob/main/copy/urls.md). Do not hardcode variants.

Community discussion happens in the [Portolan Google Group](https://groups.google.com/g/portolan) and the [Portolan channel](https://cloudnativegeo.slack.com/archives/C0A1JBH9529) in Cloud-Native Geo Slack. Planning happens in [org-level GitHub projects](https://github.com/orgs/portolan-sdi/projects/1).

## Contribution rules

The [AI policy](https://github.com/portolan-sdi/portolan-ops/blob/main/policies/AI_POLICY.md) applies to every contribution. An agent may draft the diff and the pull request body. A human must read, understand, and approve both before review is requested. Agents never open PRs, post comments, or take action in shared spaces without human approval.

Follow the [contributing guide](https://github.com/portolan-sdi/portolan-ops/blob/main/policies/CONTRIBUTING.md) and the [code of conduct](https://github.com/portolan-sdi/portolan-ops/blob/main/policies/CODE_OF_CONDUCT.md).

## Sync discipline

Files between `ops-sync` markers are synced from [portolan-ops](https://github.com/portolan-sdi/portolan-ops). They are overwritten on every sync run. To change one, edit it in portolan-ops, never in place.

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
