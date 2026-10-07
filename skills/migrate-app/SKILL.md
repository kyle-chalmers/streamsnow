---
name: migrate-app
description: Port an external Streamlit app into the repo in two reviewable steps — lift-and-shift into apps/<slug>/, then conform to repo conventions until validation passes. Use when the user says "migrate a streamlit app", "port this dashboard into the repo", "bring an external app in", or "modernize this dashboard".
argument-hint: "<path-or-repo> [<slug>]"
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep]
disable-model-invocation: true
---

# /migrate-app

> **Repo overlay:** if `.streamsnow/overlays/migrate-app.md` exists in this repo, read it first — committed, repo-specific additions/overrides ([_shared/overlays.md](../_shared/overlays.md)). Outside Claude Code, also read [_shared/other-agents.md](../_shared/other-agents.md).

Bring an external Streamlit app into the repo, then conform it until the gates pass. **Two
commits** — the lift (files relocated, only ship blockers scrubbed), then the conform diff — because
mixing the move and the rewrite makes review impossible and kills your ability to bisect a
misbehaving conform against the known-good lift.

**This skill gets the app in**; [/build-app](../build-app/SKILL.md)'s phases make it good, so a
migrated app meets the same bar as a new one; the gates (`streamsnow validate-app`,
`streamsnow sql-review check`) decide "done".

Detection is `streamsnow migrate <verb>` — JSON out, deterministic, AST-only (never executes
source). You read the JSON and make the judgment calls. Read [engine.md](engine.md) before Step 1 —
it documents every verb: when to run it, what its JSON says, the judgment call that follows.

## Step 1 — lift-and-shift (get it in the tree)

1. `streamsnow doctor` first; failures → `/onboard` before continuing.
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

## Step 2 — conform (make it a StreamSnow app, the /build-app way)

7. **Spec it:** follow build-app [spec.md](../build-app/spec.md) in backfill mode for
   `apps/<slug>/REQUIREMENTS.md` (pages, their questions, sources, TTLs, the runtime from step 5).
8. **Foundation:** `streamsnow new` the same slug and runtime in a scratch repo; copy in every file
   the lift lacks (`snowflake.yml`, `branding.py`, `sql_loader.py`, `review.py`, `pages/_*.py`,
   `pages/about.py`, `sql_review/`), never over a source file; drop its starter page and query
   ([pages.md](../build-app/pages.md#replace-the-starter-trio)). Then [scaffold.md § Foundation](../build-app/scaffold.md#foundation-after-the-scaffold-before-any-page).
   Keep imports app-local (`from branding import ...`): deployed, a repo-level `shared/` is gone.
9. **Conform the pages:** [pages.md § Parallel build](../build-app/pages.md#parallel-build) with
   each page-builder in `mode: conform` (keep the visuals and behavior, apply the conventions).
   Its worklist is the JSON of `streamsnow migrate scan-conformance apps/<slug>` and
   `streamsnow migrate scan-inline-sql apps/<slug>`, split by file. Migrate-only items
   ([engine.md](engine.md)): pin columns for each `SELECT *`, swap `altair_imports` to the repo
   chart standard, rebuild navigation with `st.navigation` if `legacy_pages_only`, and put the
   `required_grants` needing a DBA in the PR. **Walk each query's Feeds/Schemas with the user**;
   plumbing SQL stays inline with `# noqa: inline-sql`.
10. Check `snowflake.yml` matches the runtime; scrub personal absolute paths the copy brought along.
    Then, as /build-app does ([pages.md](../build-app/pages.md#one-page)), rewrite the app `AGENTS.md` starter Pages and Queries lines to the real pages and queries (noting non-default TTLs and the runtime decision there)
    and add the repo README Apps row (title and `apps/<slug>/`); validate-app's `starter-text` check warns otherwise.
11. **Verify:** build-app [verify.md](../build-app/verify.md), then the user clicks through every page in /preview-app.
    A warehouse app failing locally on `get_active_session` is the runtime's signature, not a bug.
12. **Gate:** `streamsnow validate-app <slug>` PASS, `streamsnow sql-review check <slug>` clean,
    and both conform scans' fix-lists empty. Then commit the conform as its own changeset. A
    deploy error you can't place → [_shared/deploy-error-translator.md](../_shared/deploy-error-translator.md).

## Hand-offs

PASS → /ship-app opens the PR (first-time accounts may need one-time `streamsnow deploy-setup`
DDL); deeper quality → /review-app + /sql-review; an app already in the repo → Step 2 only;
later changes → `/build-app <slug>` or `/build-app <slug> --feedback "..."`.

## Done when

`streamsnow validate-app <slug>` passes, `streamsnow sql-review check <slug>` is clean, the two
conform scans report nothing to fix, `REQUIREMENTS.md` describes the app, and the lift and
conform are two separate commits.
