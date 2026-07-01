---
name: ship-app
description: Stage, commit, push, and open a PR for a StreamSnow app, gated on a passing validation. Use when the user says "ship it", "ship this app", "open a PR", "deploy <slug>", or follows /preview-app and wants to publish. Runs /validate-app as a hard gate first and watches PR checks to a terminal state.
---

# ship-app

Take a built app from working tree to open PR, gated on a deterministic validation, then watch CI to a terminal state. Scope is intentionally narrow: **one app at a time** (`apps/<slug>/`), and deployment is **CI-only on merge to `main`** — this skill never runs a local Snowflake deploy.

## Before you start

- This skill ships changes scoped to a single `apps/<slug>/` directory (plus any sibling file that is genuinely part of the same change, e.g. the repo `README`'s app-index row). Repo-level changes — templates, governance files, shared skills/recipes, CI — do **not** belong in a `/ship-app` PR; commit those separately.
- The deploy is the generated CI pipeline's job on merge to `main`, not yours. The one-time Snowflake DDL (`streamsnow deploy-setup`) must already be applied — that is an /onboard concern, not a per-ship one.

## Steps

1. **Resolve the slug.** If absent, list `apps/*/` and ask which one; confirm `apps/<slug>/` exists. Run `git status --short apps/<slug>` to confirm there are working-tree changes to ship. Zero changes ahead of `main` → there is nothing to PR; stop and say so.

2. **Hard gate — validate.** Run `/validate-app <slug>` (which runs `streamsnow validate-app <slug>`, the single source of truth: files, schema-refs, security, caching, bind-predicates). On any FAIL, **stop** — report the failing checks and do not stage, commit, or push. `/validate-app` is the fix-it path; do not auto-fix here. Continue only on PASS.

3. **Branch hygiene.** If on `main`, branch first: `git switch -c ship/<slug>-<short-desc>`. **Never reuse an already-merged branch** — a squash-merged branch reused for a second PR can make Git's three-way merge silently revert your own deletions. Check before continuing:
   `gh pr list --search "head:$(git branch --show-current) is:merged" --json number`
   Non-empty → the branch is spent; start fresh off `main` and re-apply the work (cherry-pick or copy edits — never `git merge` from the old branch).

4. **Stage only the app's changes.** `git add apps/<slug>` (plus the repo `README` only if its app-index row changed). Show `git diff --cached --stat` so the user sees exactly what will commit. If any other path is staged, unstage it — `/ship-app` is not the primitive for repo-level changes.

5. **Commit** with a conventional message: `feat(<slug>): <summary>` (or `fix(...)`/`docs(...)` as fits). Keep the summary to the user-visible deliverables — underclaim, never overclaim. If the pre-commit hooks block (schema-refs, security, caching, secrets, lint), **stop and surface the error** — never `--no-verify`. A hook failure is a real finding the same governance checks would catch in CI anyway.

6. **Sync with `origin/main` before pushing.** Follow `_shared/sync-with-main.md`: rebase (never merge) onto current `origin/main`, then `git push --force-with-lease`. Skipping this lets a PR open already out-of-date when `main` moved during development, which branch protection then silently blocks. On a rebase conflict the recipe stops and hands you manual instructions — do not guess a resolution.

7. **Push** (if the sync step did not already force-push the rebased branch): `git push -u origin HEAD`. Refuse to push directly to `main`/`master`.

8. **Open the PR.** `gh pr create --fill`, or write a title/body summarizing what changed (SQL added/changed, pages added, caching choices) and the validation that passed. Capture the PR number and URL and print them.

9. **Note the deploy path.** Tell the user: merging to `main` triggers the CI pipeline, which deploys the app to Snowflake. There is no local deploy step. (Mechanically, CI stages the app code at a commit-SHA path and runs the `CREATE OR REPLACE STREAMLIT` that `streamsnow deploy-sql <slug>` would emit — you do not run it by hand.)

10. **Watch PR checks to a terminal state.** Run the watch loop in the background and report once on exit:
    `gh pr checks <num> --watch` and `gh pr view <num> --json state,mergeStateStatus`.

11. **Report the outcome:**
    - A check fails → name the failed check, translate the failure if it is deploy-related (see `_shared/deploy-error-translator.md`), and stop.
    - Checks pass but unmerged → the PR needs a teammate's approval (you cannot approve your own).
    - Merged → confirm, then report the deploy run's outcome.

## Runtime note (warehouse vs container)

The runtime an app declares (warehouse-backed vs container/compute-pool) is set during `/new-app` / `/start-app` and lives in the app's Snowflake manifest — `/ship-app` does not change it and should not try to. It matters here only for how a *deploy failure* reads:

- **Warehouse-backed** apps fail fast and loud — almost always a missing grant on a queried schema, surfaced as `Insufficient privileges` / `not authorized`.
- **Container** apps add cold-start and image-build failure modes — a missing/suspended compute pool, or a missing external access integration blocking PyPI during the image build. A container deploy can also report `failure` only because the verify step outran a 1–3 min cold start while the STREAMLIT object was created fine.

When a deploy fails, route the log through `_shared/deploy-error-translator.md` rather than echoing the stack trace — it maps the signature to a plain-English cause, names the right config object, and tells the user whether the app actually shipped.

## Gotchas

- **Squash-merged branch reuse is the highest-severity trap.** It fails silently — CI passes, the deploy ships the wrong code. The Step 3 check is non-negotiable; on a non-empty result, refuse and start fresh.
- **Never `--no-verify`.** The pre-commit hooks run the same governance checks (the `streamsnow check` subcommands `schema-refs`, `security`, `caching`, `bind-predicates`) that gate CI. Bypassing locally just moves the failure to CI.
- **Don't widen the staging scope to "fix one more thing."** A non-`apps/<slug>/` path sneaking into the commit is the usual cause of a confusing review. Commit unrelated changes on their own branch.
- **Commit message must match the diff.** If the message claims "adds X tab" but the diff has no matching hunk, the message is lying to the reviewer — re-read the diff and correct one or the other before opening the PR. This catches a lost fix from a botched manual conflict resolution even when Step 3 passed.
- **You can't approve your own PR.** A green-but-unmerged PR is waiting on a human reviewer, not on you — say so plainly rather than looping on the checks.

## Troubleshooting

- **`validate-app` FAILs** → run `/validate-app <slug>`; it re-runs the focused gate (`streamsnow check <name> apps/<slug>`) to surface the exact offending lines and fixes. Return to Step 2 only after PASS.
- **Pre-commit hook blocks the commit** → read the hook's output; it names the file/line and the rule. Fix the code (or re-run the matching `streamsnow check`), then re-commit. Do not bypass.
- **Push rejected (`--force-with-lease` stale info)** → a teammate pushed to your branch after your last fetch. Re-run the sync recipe (`git fetch` → rebase → push); do not escalate to plain `--force`.
- **PR opens out-of-date / merge blocked as "behind"** → `main` moved; re-run Step 6 to rebase and force-push, then let checks re-run.
- **Deploy run ends `failure`** → translate via `_shared/deploy-error-translator.md`. Most fixes are one-time, owner-applied DDL (a grant, a compute pool, an external access integration) emitted by `streamsnow deploy-setup` — surface the named fix; don't try to run DDL from here.

## Done when

The PR is open, `/validate-app` passed before staging, the branch is rebased on current `origin/main`, and PR checks have reached a terminal state with the outcome reported to the user: a named failed check, "awaiting teammate approval," or merged + the deploy run's result.

## Hand-offs & references

- **Inline gate:** /validate-app — the deterministic PASS/FAIL `streamsnow validate-app` gate this skill runs first. Never bypass it.
- **Pre-ship quality pass:** /review-app for qualitative concerns (SQL efficiency, UI patterns, spec drift) the deterministic gate doesn't judge; /preview-app to run the app locally against live Snowflake before shipping.
- **Branch sync (pre-push + stale-branch recovery):** `_shared/sync-with-main.md`.
- **Deploy/CI failure translation (post-merge):** `_shared/deploy-error-translator.md`.
- **First-time setup:** /onboard for machine prerequisites and the one-time `streamsnow deploy-setup` DDL that the CI deploy depends on.
