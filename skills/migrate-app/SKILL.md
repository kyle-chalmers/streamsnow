---
name: migrate-app
description: Port an external Streamlit app into the repo in two reviewable steps — lift-and-shift into apps/<slug>/, then conform to repo conventions until validation passes. Use when the user says "migrate a streamlit app", "port this dashboard into the repo", "bring an external app in", or "modernize this dashboard".
argument-hint: "<path-or-repo> [<slug>]"
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep]
disable-model-invocation: true
---

# /migrate-app

> **Repo overlay:** if `.streamsnow/overlays/migrate-app.md` exists in this repo, read it first — committed, repo-specific additions/overrides ([_shared/overlays.md](../_shared/overlays.md)).

Bring an external Streamlit app into the repo, then conform it to StreamSnow conventions until the
validation check passes. **Two commits** — the lift (files relocated, only ship blockers scrubbed),
then the conform diff — because mixing the move and the rewrite makes review impossible and kills
your ability to bisect a misbehaving conform against the known-good lift.

Detection is `streamsnow migrate <verb>` — JSON out, deterministic, AST-only (never executes
source). You read the JSON and make the judgment calls. Read [engine.md](engine.md) before Step 1 —
it documents every verb: when to run it, what its JSON says, the judgment call that follows.

## Step 1 — lift-and-shift (get it in the tree)

1. `streamsnow doctor` first; failures → `/start-app --setup` before continuing.
2. Settle `<slug>` (`<domain>-<function>`, kebab-case), then
   `streamsnow migrate preflight <source> --target-slug <slug>`. Exit 1 → stop and resolve with
   the user (`abort_reason` names it: target exists, not a Streamlit app, multiple entrypoints, or
   catastrophic deps that force a runtime conversation).
3. `streamsnow migrate graft-plan <source>` and `streamsnow migrate scan-imports <source>` —
   the first says where the UI lands (`pages/*`, `pages/overview.py`, or `streamlit_app.py`, with
   the reason), the second names the subpackages that must be grafted **whole** so relative imports
   survive. Copy the source in accordingly; the one structural change is renaming the entrypoint to
   `streamlit_app.py` (not configurable). No refactoring yet.
4. `streamsnow migrate scan-hardfails <source>` and clear every block before committing:
   denied-schema references re-pointed to governed equivalents (with the user — the mapping can't
   be guessed), hardcoded secrets out of `.py` (the user copies values by hand into gitignored
   `secrets.toml`; never read or commit a source secrets file — the scanner itself only
   presence-checks them). Re-run until exit 0.
5. Dependencies: **decide the runtime with the user** per
   [_shared/runtime-decision.md](../_shared/runtime-decision.md) — it drives the manifest,
   connection pattern, and which packages even exist. Warehouse →
   `streamsnow migrate translate-deps <source> --out apps/<slug>/environment.yml` writes the
   conda manifest (walk `dropped`/`unmapped`/`inferred_suggestions` with the user — nothing is
   auto-added). Container → declare PyPI deps in `pyproject.toml`, using the same JSON as inventory.
6. Commit the lift as one changeset.

## Step 2 — conform (make it a StreamSnow app)

7. Source the canonical local helpers (`branding.py`, `sql_loader.py`) from a scratch
   `streamsnow new` or a sibling app. Always `from branding import ...` — never `from shared...`;
   the deployed runtime can't reach a repo-level `shared/`, so a shared import runs locally and
   fails at deploy.
8. `streamsnow migrate scan-conformance apps/<slug>` is the worklist: wrap each `uncached_queries`
   fetch in `@st.cache_data(ttl=...)` (filters as function arguments, not closures), pin explicit
   columns per `select_stars` entry, swap `altair_imports` to the repo chart standard, rebuild
   navigation if `legacy_pages_only`, and surface the `required_grants` split in the PR. Re-run
   until the three fix-lists empty and `legacy_pages_only` is false ([engine.md](engine.md)).
9. `streamsnow migrate scan-inline-sql apps/<slug>` lists the SQL to externalize into
   `queries/<name>.sql` with the required header block (`Query / Feeds / Schemas / Params /
   Tokens`) behind `load_sql`/`render_sql`. **Walk the Feeds/Schemas mapping with the user one
   candidate at a time**; plumbing queries stay inline with `# noqa: inline-sql`. Replace optional
   `(:N IS NULL OR col = :N)` predicates with `{TOKEN}` fragments while you're in there — deployed,
   the driver NULL-binds every parameter when any one is `None`
   (`streamsnow check bind-predicates` catches it).
10. Write `snowflake.yml` for the chosen runtime; add an app `AGENTS.md` noting non-default TTLs
    and the runtime decision; scrub personal absolute paths the copy brought along.
11. **Bootstrap the audit trail** (details in [engine.md](engine.md)):
    `streamsnow sql-review discover <slug> --write`, author the skeleton manifests (real sample
    fragments, not the TODO placeholders), then `streamsnow sql-review generate <slug>` and
    `streamsnow sql-review index <slug>` — the review SQL ships inside the conform commit.
12. Preview via /preview-app so the user confirms each page still renders. A warehouse app failing
    locally on `get_active_session` is the runtime's signature, not a bug.
13. Gate: `streamsnow validate-app <slug>` until PASS, then commit the conform pass as its own
    changeset. A deploy error you can't place →
    [_shared/deploy-error-translator.md](../_shared/deploy-error-translator.md).

## Hand-offs

PASS → /ship-app opens the PR (first-time accounts may need one-time `streamsnow deploy-setup`
DDL); deeper quality → /review-app + /audit-lineage; an app already in the repo → Step 2 only.

## Done when

`apps/<slug>/` holds the conformed app — local helpers, headered `queries/*.sql`, a
runtime-matching `snowflake.yml`, every fetch cached, a generated `sql_review/` audit trail, zero
findings from the two conform scans — `streamsnow validate-app <slug>` passes, and the lift and
conform are two separate commits.
