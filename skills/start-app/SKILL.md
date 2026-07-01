---
name: start-app
description: End-to-end playbook for building a StreamSnow app from spec to PR — walks the human through refine, scaffold, build, preview, validate, review, and ship with checkpoints. Use when the user says "build a full app end to end", "do the whole pipeline", "ship a new dashboard start to finish", or "run the whole chain".
---

# /start-app

One playbook that walks a new StreamSnow app from requirements through a shipped PR, pausing at three checkpoints for human sign-off. It is the *outer* skill — it sequences the sibling skills below, verifies structure between phases, and tracks resumable state so an interrupted session can pick up where it left off.

> **Wizard, not a runner.** Skills can't dispatch each other as slash commands. `/start-app` does not silently invoke other skills — at each step it tells the user the exact `/skill` to run, ends its turn, and waits. When the user returns, it runs a read-only structural check, updates state, and prompts the next step. The win is "less to remember", not "less to type".

## The chain at a glance

| Phase | What runs (skill runs `streamsnow …`; user runs the `/`skills) | Checkpoint after |
|---|---|---|
| 1. Spec & scaffold | `/refine-requirements` → `streamsnow new` (or `/new-app`) | **CP1** — review scaffold tree + §4 Pages |
| 2. Build pages | `/add-page <slug> <page>` (one per §4 entry), then fill query/chart/KPI stubs | **CP2** — browser preview verification |
| 3. Validate & ship | `streamsnow validate-app` → `/auto-review-app` (or `/review-app` + `/apply-review`) → `/ship-app` | **CP3** — ship-readiness |

## Steps

1. **Prereqs.** Run `streamsnow doctor`. If anything fails, fix it before proceeding; if the Snowflake env or governance config isn't set up yet, run `streamsnow configure` (or `/onboard` for a guided machine-and-repo setup). Both are idempotent — re-running edits rather than restarts.
2. **Requirements.** Tell the user to run `/refine-requirements` (existing slug, a ticket id from your tracker, or `--new` to start fresh). When they return, confirm a `REQUIREMENTS.md` exists with **§4 Pages** and **§11 Build Progress** populated. §11 is the resume contract for the whole run.
3. **Scaffold.** Run `streamsnow new <domain> <function>` (or `/new-app`) to create `apps/<slug>/`. Derive `<domain>`/`<function>` from §1 of the spec; the slug is `<domain>-<function>`. If the staged spec lives outside the app dir, have the user `git mv` it to `apps/<slug>/REQUIREMENTS.md` so §11 travels with the app. Confirm `apps/<slug>/streamlit_app.py` exists.
4. **CHECKPOINT 1 — post-scaffold.** Show the scaffold tree and the §4 page list (which pages the scaffold already created vs. which are TODO). Suggest an early `/review-app <slug>` + `/apply-review <slug>` structural pass. Ask: continue / pause-to-edit-spec / abort. Block until the user chooses.
5. **Build pages.** For each TODO page in §4 order, tell the user to run `/add-page <slug> <page>`. After scaffolding, fill the generated query/chart/KPI stubs against the spec. Verify each `pages/<file>.py` lands before moving on. Wrap every data-fetch in `@st.cache_data(ttl=...)` and query only allowed schemas (see Gotchas) — both are validate-gate failures otherwise.
6. **Preview.** Tell the user to run `/preview-app <slug>` (or `streamsnow preview <slug> [--port N]`) against live Snowflake. `streamsnow preview` reads `.streamlit/secrets.toml`; use `--dry-run` to print the launch command without starting. If a Playwright walkthrough is available, the preview step drives every nav-registered page programmatically first — see `skills/_shared/playwright-walkthrough.md`.
7. **CHECKPOINT 2 — browser verify.** The user opens the local URL and confirms each page renders, charts populate, and filters work — the data-correctness checks no static gate can make. Block until they confirm: looks-good / found-issues / preview-broken.
8. **Validate gate.** Run `streamsnow validate-app <slug>` (or `/validate-app <slug>`) — the deterministic PASS/FAIL ship gate. Any FAIL aborts the ship; fix and re-run. To isolate one failing gate, run the matching governance check directly: `streamsnow check schema-refs|security|caching|bind-predicates [paths]` (defaults to scanning `apps/`; add `--format json` to parse).
9. **Final review.** Tell the user to run `/auto-review-app <slug>` (the auto-fix loop) or `/review-app <slug>` + `/apply-review <slug>` until findings are clean. Reviews can fan out to additional agents — see `skills/_shared/cross-agent-review.md`.
10. **CHECKPOINT 3 — ship-readiness.** Confirm `validate-app` PASSes, review is clean, and the user is ready. Then hand off to `/ship-app <slug>` (stage, commit, push, open PR). A first-time deploy may need a one-time `streamsnow deploy-setup` to emit the Snowflake DDL for the configured deploy source — pipe it to an admin/CI role and review before running.
11. **Resume.** §11 Build Progress is the contract — re-read it to pick up an interrupted run at the last completed step.

## State tracking — §11 Build Progress

State lives **inside** the app at `apps/<slug>/REQUIREMENTS.md` §11, not in a sidecar file. It is checked into git, human-readable, and survives reboots, branch switches, and different machines — any future session that opens the file sees the live build state.

On every state-changing transition, update §11 surgically (via `Edit`, not a full rewrite):
- Bump `Current phase` (`spec → scaffold → build → preview → verify → ship → done`) and `Last updated` (UTC ISO).
- Set `Last action` to the skill that just completed.
- Move the relevant `Pages` row forward (`pending → scaffolded → done`).
- Append a `Sessions` line (audit trail — never rewrite past entries).
- Refresh the `Resume hint` with the next concrete command.

On `--resume` (or noticing §11 already exists), read `Current phase` and jump to the matching step: `spec`→Step 2 (requirements), `scaffold`→CP1, `build`→the `/add-page` loop at the first pending page, `preview`→CP2, `verify`→Step 8, `ship`→Step 10, `done`→report already shipped. If §11 is missing on an older spec, append it with `Current phase: unknown` and ask the user where they are.

## Decision guidance

- **Runtime: container vs. warehouse.** The scaffold sets a runtime in the app's `snowflake.yml`; the repo's configured default lives in `streamsnow.config.yaml`. **Container** is the modern default for most new apps — full PyPI deps, modern Streamlit, and `st.connection("snowflake")` behaves the same locally and deployed (so preview catches grant gaps); it needs a compute pool + external-access integration. Choose **warehouse** (legacy) when you want instant cold start, Anaconda-channel deps, and no compute-pool cost. Follow the repo default unless the spec says otherwise; switching later means a re-deploy, so decide before CHECKPOINT 1.
- **Scaffold via CLI vs. `/new-app`.** `streamsnow new` is the bare scaffold; `/new-app` is the guided wrapper that also reads the staged spec and carries the runtime preference forward. Prefer `/new-app` inside this pipeline; reach for `streamsnow new` only for a quick throwaway.
- **`/auto-review-app` vs. manual review.** Use `/auto-review-app` for the hands-off fix loop. Drop to `/review-app` + `/apply-review` when you want to inspect each finding before it's applied (apply-review commits each fix separately).

## Gotchas

- **Denied schemas fail the gate.** App SQL may only reference schemas allowed by `governance.schema_allow` / `schema_deny` in `streamsnow.config.yaml` (within `governance.database`). A reference to a denied schema fails `check schema-refs`. Point queries at the allowed reporting/analytics schemas, never raw or restricted ones.
- **Missing cache TTL fails the gate.** Every data-fetching function needs `@st.cache_data(ttl=...)`; `check caching` blocks otherwise. Add it as you write each query, not at the end.
- **Security check blocks egress / code-exec / write-SQL / dynamic-SQL.** Apps are read-only dashboards. Outbound network calls, `eval`/`exec`, write DML, and string-built dynamic SQL all fail `check security`.
- **The bind-predicate trap.** `check bind-predicates` blocks the `:N IS NULL OR ...` pattern that silently breaks under the driver. Use proper parameter binding for optional filters instead.
- **Preview needs secrets.** `streamsnow preview` reads `.streamlit/secrets.toml`. If preview can't connect, confirm that file exists and `streamsnow doctor` passes before debugging the app.

## Troubleshooting

- **`streamsnow new` says the app already exists.** A prior run left a half-scaffolded app. Read its §11 — if `Current phase` isn't `done`, resume; otherwise have the user clean up before retrying. Only pass `--force` deliberately, since it overwrites scaffold files.
- **`validate-app` FAILs.** Run the single matching `streamsnow check <kind>` to see the exact offending lines, fix, then re-run `validate-app`. Don't ship on a FAIL.
- **Preview renders blank or errors on one page.** Check that the page's query references only allowed schemas, that filters bind correctly, and the browser console for tracebacks (the Playwright walkthrough captures these). Fix, re-preview, then re-run the verify checkpoint.
- **`/ship-app` fails because the user is on the default branch.** `/ship-app` handles branching itself; have the user re-run it from a feature branch.
- **`deploy-setup` looks like a stub / deploy fails after merge.** The emitted DDL is the one-time setup for the configured deploy source — surface it for an admin to review and run. For decoding a post-merge deploy error, see `skills/_shared/deploy-error-translator.md`.

## Guardrails

- **Never bypass a checkpoint.** Browser verification (CP2) catches UX regressions static checks miss. The user can answer `looks-good` to advance, but the prompt always fires.
- **Never auto-merge by default.** Any auto-merge option on `/ship-app` is opt-in and respects branch protection.
- **Never overwrite state.** §11 is append-mostly — only mutate the row that changed; preserve the Sessions log. If §11 is malformed, pause and ask before rewriting.
- **Don't claim a phase succeeded without its structural check.** Each step verifies (file exists / gate passes / PR opened). If the check fails, stop.
- **Runs the CLI, hands off the interactive steps.** This skill runs the deterministic `streamsnow` CLI steps (`doctor`, `new`, `validate-app`) and read-only verification (`ls`, `test`, `grep`) itself. It does **not** run the interactive steps — `/refine-requirements`, `/add-page`, the preview (`/preview-app` / `streamsnow preview`, which the user opens in a browser for CP2), `/review-app`, `/apply-review`, `/ship-app` — it tells the user to run those, keeping a human in the loop at every judgment point and checkpoint.

## Out of scope

- **Migrating an existing app** → use `/migrate-app`; its two-step flow doesn't compose into this pipeline.
- **Adding one page to an in-production app** → just run `/add-page` directly; no orchestration needed.
- **Backfilling a spec for an existing app** → use `/refine-requirements <existing-slug>`; `/start-app` is for *new* apps.

## Done when

`/ship-app` has opened the PR, `streamsnow validate-app <slug>` PASSes, the final review is clean, and §11 reflects `Current phase: done` with a `Resume hint` pointing at watching the deploy.

## References

- Chained skills: `/refine-requirements`, `/new-app`, `/add-page`, `/preview-app`, `/validate-app`, `/review-app`, `/apply-review`, `/auto-review-app`, `/ship-app`
- Related entry points: `/onboard` (machine + repo setup), `/migrate-app` (existing apps)
- Shared recipes: `skills/_shared/playwright-walkthrough.md`, `skills/_shared/cross-agent-review.md`, `skills/_shared/deploy-error-translator.md`
