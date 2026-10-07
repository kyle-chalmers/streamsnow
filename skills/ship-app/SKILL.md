---
name: ship-app
description: Stage, commit, push, and open a PR for one app, gated on a passing validation, then watch CI to a terminal state. Use when the user says "ship it", "open a PR", "deploy <slug>", or after preview and review look good.
argument-hint: "<slug>"
allowed-tools: [Bash, Read]
disable-model-invocation: true
---

# /ship-app

> **Repo overlay:** if `.streamsnow/overlays/ship-app.md` exists in this repo, read it first — committed, repo-specific additions/overrides ([_shared/overlays.md](../_shared/overlays.md)). Outside Claude Code, also read [_shared/other-agents.md](../_shared/other-agents.md).

Take a built app from working tree to open PR, gated on validation, then watch CI. **One app at a
time**; deployment is **CI-only on merge to `main`** — never a local Snowflake deploy. Repo-level
changes (templates, governance, CI) do not belong in a `/ship-app` PR; commit those separately.

## Steps

1. **Resolve the slug**; `git status --short apps/<slug>` must show changes to ship — zero changes
   ahead of `main` → nothing to PR; stop and say so.
2. **Preflight 0: review gate (asks, never blocks).** Run
   `streamsnow review-gate classify <slug> --format json` and gate on `.apps[0].needs_review`
   (`verdict` is only review depth). False (reviewed, trivial or skip marker) → proceed. True → offer
   review first (`/review-app <slug> --auto`) or **ship as-is**, always available (ships can be
   time-critical; /validate-app + CI are the real publish gates), noting "shipped unreviewed" in the
   PR body. SQL changed → recommend `/sql-review <slug>` before merge (never required). Never
   auto-run either. Save `.apps[0].commits_since_review`, `reviewed_head_status` and the JSON from `streamsnow review-loop open-findings apps/<slug>/.review`
   now (the step 7 rebase rewrites SHAs); after a review-first pass, re-run these saves.
3. **Hard gate:** run /validate-app. Any FAIL → stop; report and do not stage, commit, or push.
   /validate-app is the fix-it path — don't auto-fix here.
4. **Branch hygiene.** On `main` → `git switch -c ship/<slug>-<desc>` first. **Never reuse a
   squash-merged branch**: a three-way merge can silently revert your own deletions. Check
   `gh pr list --search "head:$(git branch --show-current) is:merged" --json number`; non-empty =
   spent: start fresh off `main`, re-apply (cherry-pick or copy, never `git merge` the old branch),
   and after step 6 clean it up per [After merge](#after-merge). A stop there never ends the ship.
5. **Stage only the app:** `git add apps/<slug>` (plus the repo README only if its app-index row
   changed). Show `git diff --cached --stat`; unstage anything else — don't widen scope to "fix one
   more thing".
6. **Commit** conventionally (`feat(<slug>): <summary>`), the summary matching the diff —
   underclaim, never overclaim. Pre-commit hooks block → **stop and surface the error**; never
   `--no-verify` (the hooks run the same checks CI does).
7. **Sync with `origin/main` before pushing** per [sync-with-main](../_shared/sync-with-main.md):
   rebase (never merge), then `git push --force-with-lease`. A rebase conflict stops with manual
   instructions — don't guess a resolution.
8. **Push** (`git push -u origin HEAD` if the sync didn't already) — refuse to push to `main`.
9. **Open the PR** (title/body: what changed, validation passed, then `Open critical: N` and the
   commits since review from step 2, per [these rules](../review-app/report-and-stamp.md#in-the-ship-app-pr-body)). Print the number and URL.
10. **Note the deploy path:** merging to `main` triggers CI, which deploys — no local deploy step.
11. **Watch checks to a terminal state** (`gh pr checks <num> --watch` in the background;
    `gh pr view <num> --json state,mergeStateStatus`) and report once on exit. A host that forbids
    polling CI: hand the user `gh pr checks <num> --watch` and end at "PR open, checks pending".

## Reporting the outcome

- **A check fails** → name it; translate deploy failures via
  [_shared/deploy-error-translator.md](../_shared/deploy-error-translator.md) (failure signatures
  differ by runtime — see [_shared/runtime-decision.md](../_shared/runtime-decision.md)); stop.
- **Green but unmerged** → it's waiting on a teammate's approval (you can't approve your own PR) —
  say so plainly rather than looping on the checks. /ship-app never merges; it stops here.
- **Merged** → confirm, report the deploy run's outcome; green → `streamsnow app-url <slug>` per
  [click-through.md](click-through.md) (the link and checklist), then [After merge](#after-merge).

## After merge

When the watch sees `MERGED` or step 4 finds a squash-merged branch, follow [after-merge.md](after-merge.md): it asks once only when the user did not approve the merge in this run.

## Gotchas

- **Squash-merged branch reuse is the highest-severity trap** — it fails silently: CI passes, the
  deploy ships the wrong code. The step 4 check is non-negotiable.
- **Commit message must match the diff.** A claimed change with no matching hunk means a fix was
  lost (often in manual conflict resolution): re-read the diff and correct one or the other first.
- Most deploy-run failures resolve to one-time, admin-applied DDL emitted by
  `streamsnow deploy-setup --admin`: surface the named fix; never run DDL from here.
- **Push rejected** (stale `--force-with-lease`) → re-fetch, rebase, push, never plain `--force`. PR
  opens "behind" → `main` moved; re-run the sync step and let checks re-run.

## Done when

The PR is open, validation passed before staging, the branch is rebased on current `origin/main`,
and checks reached a terminal state with the outcome reported: a named failed check, "awaiting
approval," or merged + the deploy result. Polling not allowed: "PR open, checks pending". Merged:
local checkout on updated `main`, spent branch removed.

## System-evolution retro (always, even on a clean ship)

Before closing, ask what went wrong or got re-done and which layer fell short (config, a skill, a check or the deploy path). Fix that layer, not the instance: [retro.md](retro.md).
