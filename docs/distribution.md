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
   expert for this stack (`/new-app`, `/preview-app`, `/validate-app`,
   `/ship-app`, `/start-app`, …). Served straight from the public repo — no
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

## See also

- [Getting started](getting-started.md) — install and scaffold your first app.
- [Deploying](deploying.md) — ship apps to Snowflake.
- [`RELEASING.md`](../RELEASING.md) — how a `streamsnow` release is cut (tag →
  PyPI via Trusted Publishing); the plugin needs no publish step.
