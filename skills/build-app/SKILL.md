---
name: build-app
description: The front door for building a Streamlit-in-Snowflake app from idea to opened PR, or resuming one mid-build. Orchestrates spec, data discovery, page design, scaffold, a parallel page build and review by subagents, and ship, with human checkpoints between them. Start here for any new app, to document an existing one, to add a page, or to act on feedback about a live app. Use when the user says "build an app", "new dashboard", "add a page", "spec this out", "pick up where we left off", "here's feedback on <app>", "the numbers look wrong" or "polish this dashboard".
argument-hint: "[<idea>] | --spec | <slug> --feedback \"<feedback>\""
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion, Task, Agent]
---

# /build-app

> **Repo overlay:** if `.streamsnow/overlays/build-app.md` exists in this repo, read it first — committed, repo-specific additions/overrides ([_shared/overlays.md](../_shared/overlays.md)). Outside Claude Code, also read [_shared/other-agents.md](../_shared/other-agents.md).

Lifecycle: **spec → discover → design → scaffold → build → preview → verify → ship → done**.
It reads `apps/<slug>/REQUIREMENTS.md` §11 to resume and never skips a checkpoint.

> **Orchestrator.** You run every deterministic step (CLI, merges, generation, validation) and own
> the shared files; subagents get fixed briefs ([briefs/](briefs/)) naming their inputs, the only
> files they may write, and the result they return. The CLI and gates decide pass/fail, never a
> subagent. No subagents? Follow each brief yourself ([_shared/other-agents.md](../_shared/other-agents.md)).

## Modes

- **Default** — a new app, or a slug to resume from §11 `Current phase`; never restart a build.
- **`--spec [<slug>]`** — write or refresh the spec only, then stop. Covers new specs, tickets and
  **backfill** from an existing app's source (automatic when `apps/<slug>/` has code). [spec.md](spec.md).
- **`<slug> --feedback "<feedback>"`** — classify feedback on a live app, then fix it via [feedback.md](feedback.md).

## Phase 0 · Preflight

1. Report which `streamsnow` runs and its version, then run `streamsnow doctor --format json`.
2. `streamsnow` not on PATH, a `required` check failing, or no `streamsnow.config.yaml`: say "This
   machine or repo isn't set up yet, so I'm running onboarding first", follow
   [/onboard](../onboard/SKILL.md) in full, then continue (`--spec` may carry on without config).
   `optional` failures get one line.

## Phase 1 — Spec, then CHECKPOINT 1

3. Follow [spec.md](spec.md) for `apps/<slug>/REQUIREMENTS.md`. **CP1:** confirm its one-screen
   summary (pages and their questions, sources, TTLs, runtime). Block until confirmed.

## Phase 2 — Discover and design, then CHECKPOINT 1b

4. Follow [design.md](design.md): **data-scout** profiles the data into §3, **app-designer** plans
   every page (forms, copy, glossary, shared data) into §4–§8. **CP1b:** the text wireframe.

## Phase 3 — Scaffold the foundation

5. Follow [scaffold.md](scaffold.md): `streamsnow new <domain> <function>` (runtime from §9,
   [_shared/runtime-decision.md](../_shared/runtime-decision.md)), then the shared layer from the
   design: glossary, `pages/_data.py` loaders and their queries, About page. Commit it.

## Phase 4 — Build pages in parallel

6. Follow [pages.md § Parallel build](pages.md#parallel-build): one **page-builder** per §4 page,
   dispatched in one message; each writes only its page and its own queries and returns its nav,
   index and glossary entries. Merge them, run `streamsnow sql-review generate <slug>` once, and
   commit the round: pages and their review SQL land together.

## Phase 5 — Preview, verify, then CHECKPOINT 2

7. Follow [verify.md](verify.md): preview and walkthrough, then **perf-reviewer**, **viz-critic**
   and **cold-reader** in parallel; fixes go to each file's owner, at most two rounds.
   **CP2:** the user clicks through every page. Block until they answer.

## Phase 6 — Check and ship, then CHECKPOINT 3

8. `streamsnow validate-app <slug>` until PASS (`/validate-app` explains each FAIL); then follow
   `/review-app`'s instructions in full and present its verdict.
9. **CP3:** validation passes, review is clean, user is ready → `/ship-app <slug>` (a first deploy
   may need admin DDL from `streamsnow deploy-setup --admin`: surface it, don't run it).

## State — §11 Build Progress

A `Current phase` line (the lifecycle above) plus an append-only `Sessions` log whose last line
names the next command. Update it on every phase change; never rewrite past lines. On resume,
jump to `Current phase`. `done` or `in-production (backfilled)` means the app is live: new §4
pages re-enter at design; feedback uses `--feedback`, review depth `/review-app`.
Apps started before 0.8 name the old `start-app` skill in that log; say `/build-app` instead.

## Out of scope

Porting an external app → `/migrate-app`; review depth → `/review-app`; live numbers →
`/sql-review`; machine, repo and Snowflake setup → `/onboard`.

## Done when

The PR is open, `streamsnow validate-app <slug>` passes, and §11 reads `Current phase: done`.
