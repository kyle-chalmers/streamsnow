# Distribution

StreamSnow ships through **two channels**, and one Python package is the single
source of truth behind both.

## The two channels

1. **PyPI CLI — `streamsnow`** (`pip install streamsnow` / `uvx streamsnow`).
   This is the substrate: the typed `streamsnow.config.yaml` model, the
   scaffolder + Jinja templates, and the governance tools (`validate-app`,
   `check schema-refs|security|caching|bind-predicates`, `deploy-*`). It
   generates a governed repo and app from your config, and runs the checks.

2. **Claude Code plugin** (`/plugin marketplace add kyle-chalmers/streamsnow`).
   The skills, subagent, and SessionStart hook that make Claude Code a domain
   expert for this stack (`/start-app`, `/preview-app`, `/validate-app`,
   `/review-app`, `/ship-app`, …). Served straight from the public repo — no
   publish step, and no install beyond adding the marketplace.

The `streamsnow` package is the **one implementation, many consumers** core: the
CLI, the plugin skills, pre-commit, and CI all call the same code. A generated
repo pins `streamsnow` as a dependency, so `streamsnow check …` runs identically
in your editor, in pre-commit, and in CI — versioned and upgrade-safe (bump the
pin to adopt a new release).

## Why there's no copy-paste (`cp -r`) kit

A recurring question is whether StreamSnow should *also* ship as a directory you
copy into a repo (`cp -r streamsnow-kit/ myrepo/`). It does not, deliberately:

- **`streamsnow init` already is the "drop in a governed repo" path** — and a
  better one. It renders the repo (AGENTS.md, CI, hooks, pre-commit, deploy
  workflow, branding, a starter app) from your **validated config**, so values
  are consistent and injection-safe. Copying static files and hand-editing them
  is strictly worse than generating from a single source of truth.
- **StreamSnow's value is executable tooling, not static files.** The governance
  checks are AST-based and carry real dependencies (`pyyaml`, `packaging`, …).
  Vendoring them as copied files into every repo would either fork the logic
  (drift — the exact thing the single-source-of-truth design avoids) or bundle
  dependencies by hand. A versioned package dependency solves this cleanly.
- **A third channel is maintenance you don't get back.** PyPI + plugin already
  cover both install-free use (`uvx streamsnow`, the plugin from the repo) and
  installed use. A copyable kit would triple the surface to keep in sync for
  marginal reach — the only scenario it uniquely serves (fully offline / no
  PyPI) is niche for a Snowflake + Streamlit + Claude Code audience that already
  needs network access for Snowflake and app dependencies.

**When to revisit:** if a concrete need appears for the checks to run *without*
installing `streamsnow` (e.g. a locked-down repo that can't take the dependency),
the right answer is a CLI feature — a `--vendor` mode that writes the check tools
into the repo — not a separately maintained copy-paste kit.

## Ownership and exit

A production fleet that evaluated adopting StreamSnow wholesale paused with this
objection, quoted here because it is fair: finishing the migration would replace
roughly ten thousand lines of in-tree, readable tooling with "a PyPI package and
a Claude marketplace maintained by one person on a personal GitHub account.
Nobody else can patch it." Prose does not fix bus factor. What follows is what
is actually true today, so a team can decide with open eyes.

- **Maintainer model.** One maintainer, releases cut from `main` by tag
  ([RELEASING.md](../RELEASING.md)). Co-maintainers are welcome; the
  [contributing guide](../CONTRIBUTING.md) says how.
- **Release pinning.** Generated CI installs `streamsnow>=0.7,<0.8`, so a repo
  never takes a major silently. Stricter shops pin exact (`streamsnow==0.7.0`)
  and bump on purpose.
- **Rollback.** `uv tool install streamsnow==<previous>` and
  `streamsnow update --apply` re-render the governance files from that
  version's templates. Nothing StreamSnow writes is opaque.
- **What a repo keeps if it stops.** Every generated file — `AGENTS.md`, the
  scaffolded apps, `streamsnow.config.yaml`, the CI and deploy workflows,
  `.streamsnow/overlays/`, the `sql_review/` audit trails — is a plain file the
  repo owns and can edit. The only dependency is the `streamsnow check …` /
  `validate-app` / `sql-review` commands the pre-commit and CI configs call.
- **What does not exist yet.** A `--vendor` mode that writes those check tools
  into the repo so the dependency can be dropped. It is the planned exit path
  (see "When to revisit" above), not a shipped one, and this section will say
  so until it ships.

## See also

- [Getting started](getting-started.md) — install and scaffold your first app.
- [Deploying](deploying.md) — ship apps to Snowflake.
- [`RELEASING.md`](../RELEASING.md) — how a `streamsnow` release is cut (tag →
  PyPI via Trusted Publishing); the plugin needs no publish step.
