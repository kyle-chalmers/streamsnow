---
name: start-app
description: The front door — build a Streamlit-in-Snowflake app from idea to opened PR, or resume one mid-build. Owns the spec, scaffold, page-building, and ship phases, with human checkpoints between them. Start here for any new app, to document an existing one, to add a page, or to set up a machine or repo. Use when the user says "build an app", "new dashboard", "add a page", "spec this out", "set me up", or "pick up where we left off".
---

# /start-app

One command owns the app lifecycle: **spec → scaffold → build → preview → verify → ship → done**.
It reads `apps/<slug>/REQUIREMENTS.md` §11 to resume an interrupted build, tells the user the exact
next command at each judgment point, and never skips a checkpoint.

> **Wizard, not a runner.** It runs the deterministic `streamsnow` CLI steps and read-only checks
> itself; for interactive steps (preview click-through, review, ship) it names the exact command,
> ends its turn, and waits. The win is "less to remember", not "less to type".

## Modes

- **Default** — a new app, or a slug to resume. If `apps/<slug>/REQUIREMENTS.md` §11 exists, resume
  from its `Current phase`; never restart a build that's underway.
- **`--spec [<slug>]`** — write or refresh the requirements spec only, then stop for review. Covers
  brand-new specs, ticket ingestion, and **backfill** (reverse-engineering the spec from an existing
  app's source — automatic when `apps/<slug>/` already has code). Follow [spec.md](spec.md).
- **`--setup`** — first-time machine + repo setup: prerequisites, then `streamsnow configure`
  (≤5 questions; everything else is a commented default in the config). Follow [setup.md](setup.md).
- **`adopt`** — the repo already has Streamlit apps or its own Claude commands that didn't come from
  StreamSnow. Map onto what exists instead of scaffolding over it, and write a `MIGRATION.md`
  checklist. Follow [adopt.md](adopt.md).

## Phase 0 — Preflight (degrade, don't die)

1. Run `streamsnow doctor`. Fix-or-skip each failure interactively per [setup.md](setup.md).
2. If `streamsnow.config.yaml` is missing, this isn't a governed repo yet — offer `--setup` (or
   `adopt` if the repo already has apps). You can still write a spec without config; note that
   schema choices in §3 stay unverified until the repo is configured.

## Phase 1 — Spec

3. Follow [spec.md](spec.md) to produce `apps/<slug>/REQUIREMENTS.md` — the contract every later
   phase builds and audits against. Confirm the one-screen summary with the user before moving on.

## Phase 2 — Scaffold, then CHECKPOINT 1

4. Follow [scaffold.md](scaffold.md): `streamsnow new <domain> <function>` plus the first-build
   conventions. Runtime comes from the spec §9 / repo default — see
   [_shared/runtime-decision.md](../_shared/runtime-decision.md); decide before scaffolding.
5. **CP1:** show the scaffold tree and the §4 page list (created vs. TODO). Ask: continue /
   edit the spec / stop. Block until the user chooses.

## Phase 3 — Build pages, then CHECKPOINT 2

6. For each TODO page in §4 order, follow [pages.md](pages.md) — page module, `queries/*.sql` with
   header blocks, `st.navigation` registration — then fill the stubs against the spec. Run the
   matching `streamsnow check` commands as you go; problems are cheapest here.
7. Tell the user to run `/preview-app <slug>` and click through every page against live Snowflake.
8. **CP2:** the user confirms pages render, charts populate, and filters work — the correctness
   check no static gate can make. Block until they answer.

## Phase 4 — Check & ship, then CHECKPOINT 3

9. Run `streamsnow validate-app <slug>` — the pass/fail check that must be clean before shipping.
   Fix and re-run on any FAIL (`/validate-app` explains each one).
10. Tell the user to run `/review-app <slug>` (add `--auto` for the hands-off fix loop) until clean.
11. **CP3:** validation passes, review is clean, user is ready → hand off to `/ship-app <slug>`
    (a first deploy may need one-time admin DDL from `streamsnow deploy-setup` — surface, don't run).

## State — §11 Build Progress

§11 lives inside `apps/<slug>/REQUIREMENTS.md`: a `Current phase` line (the lifecycle above) plus
an append-only `Sessions` log whose last line always names the next command. Update it via `Edit`
on every phase transition — never rewrite past session lines. On resume, read `Current phase` and
jump to the matching phase; `done` or `in-production (backfilled)` means the app is live — new
§4 pages route to the build phase, anything else to `/feedback-app` or `/review-app`.

## Out of scope

Porting an external app → `/migrate-app`; feedback on a live app → `/feedback-app`; review depth → `/review-app`; live lineage → `/audit-lineage`.

## Done when

The PR is open, `streamsnow validate-app <slug>` passes, and §11 reads `Current phase: done`.
