---
name: sql-review
description: Prove an app's numbers against live Snowflake and leave a record a person signs. Builds or repairs the app's sql_review/ page files, checks objects, grants and DDL drift, runs every section as aggregates, has reviewer agents judge the logic, drops findings the evidence does not support, and writes the committed review log. Use when the user says "review the SQL", "are these numbers right", "trace the data", "audit the lineage", before a release or deploy, or from /review-app --sql. It spends warehouse credits, so run it when asked, not on your own initiative.
argument-hint: "<slug> [--page NN] [--offline] [--optimize] [--no-screen] [--role ROLE] [--warehouse WH] [--connection NAME]"
allowed-tools: [Bash, Read, Edit, Write, Glob, Grep, Task]
---

# /sql-review

> **Repo overlay:** if `.streamsnow/overlays/sql-review.md` exists in this repo, read it first — committed, repo-specific additions/overrides ([_shared/overlays.md](../_shared/overlays.md)). Outside Claude Code, also read [_shared/other-agents.md](../_shared/other-agents.md).

Every fact comes from a `streamsnow sql-review` command as JSON with a stable `id`; agents only
judge, every finding cites those IDs, a verifier drops what the evidence does not support, and
`log` refuses anything uncited. Judgment, never a gate: `streamsnow validate-app` alone passes or
fails a ship. Run it on request and before a release or deploy.

## Preflight

1. **Resolve the slug**; read the app's `AGENTS.md` (Data notes) and `REQUIREMENTS.md`.
2. **Offline gate:** `streamsnow sql-review check <slug>`. No `index.yaml`, coverage gaps or
   `marker` findings → build the index first, per [authoring.md](authoring.md). Drift →
   `streamsnow sql-review generate <slug>`, committed on its own: the live commands refuse page
   files that do not match what the app runs. `--offline` stops here and reports the coverage.
3. **Connection:** `streamsnow doctor`. None → stop after step 2 and name the enabler
   (`streamsnow configure`, then `snow connection add`); never invent results. The live commands
   run as `snowflake.roles.ci_role` (the deployed app's role) with secondary roles off; if the
   user does not hold it, ask which role to use and pass `--role`. The commands also use
   `objects.default_warehouse`; if that role cannot use it, pass `--warehouse` with one it can.

## Facts (JSON only)

4. `streamsnow sql-review probe <slug>`: objects exist, direct grants, DDL drift, every section
   compiles with its columns. Keep the `run_id` it prints; pass `--run <run_id>` from here on.
5. `streamsnow sql-review run <slug> --run <run_id>` (`--page NN` for one page): rows, totals,
   an order-insensitive hash and timing per section, computed in Snowflake.
6. Read these outputs, never result rows. Do not run sections yourself, and never call `snow sql`
   directly: the commands guard every statement before it is sent.
7. **Screen** (skip with `--no-screen`): preview in review mode, open every page at its default
   filters, stop the preview, then `streamsnow sql-review compare <slug> --run <run_id>`, per
   [screen.md](screen.md). Its mismatches go to the page reviewers, never straight to the log.

## Judgment

8. **In parallel** (Claude Code: one message, several Task calls): agent
   `streamsnow:sql-review-page` once per page, and `streamsnow:sql-review-object` once per object
   in `sql_review/app_specific_reporting_objects/`. Without subagents, follow
   [reviewers/page.md](reviewers/page.md) and [reviewers/object.md](reviewers/object.md) yourself,
   one brief at a time. Each brief names its inputs, the one file it writes, and its result.
9. **Optimizer** ([reviewers/optimizer.md](reviewers/optimizer.md)) for each section `run` marked
   `slow` (over 10 s), or for all with `--optimize`. Every rewrite it proposes is backed by
   `streamsnow sql-review bench <slug> --run <run_id> --metric NN#n --sql-file <candidate>` with
   `equivalent: true`. It proposes diffs, never applies them, never suggests a bigger warehouse.
10. **Verifier** ([reviewers/verifier.md](reviewers/verifier.md)), a fresh agent per page batch: it
   re-reads every cited result, tries to refute each finding, and keeps or drops it with a reason.

## Log

11. If a reviewer re-ran a page, run `compare` again (offline) so its rows are not `stale`. Merge
    the kept findings into `findings.json` in the run directory, shaped per
    [findings.md](findings.md). Validate with `streamsnow sql-review log <slug> --run <run_id>
    --findings <file> --dry-run`; a refusal names the finding and why. Fix the citation or drop
    the finding; never invent evidence.
12. Write it: the same command without `--dry-run`. It writes
    `sql_review/review_log/YYYY-MM-DD_<shortsha>.md` and updates the README's latest review.
13. `streamsnow sql-review check <slug>`, then commit the log and `sql_review/README.md` alone:
    `chore(sql-review): <slug> review <YYYY-MM-DD>`. Fixes come later, in their own commits,
    after the user agrees to each.
14. **Report:** the log path, blocker / major / minor counts and the top findings. Tell the user
    the sign-off block (Reviewer, Date, Decision) is theirs to fill in. Never fill it in.

## Rules

- No row-level data or small-group breakdowns in a finding, the log, or chat: pass or fail, row
  counts, top-level totals.
- A human applies DDL. Propose it; deploy it only when the user explicitly says to.
- Data judgment follows [tracing.md](tracing.md): never claim upstream is broken without cited
  evidence, and tell "missing" from "not visible to this role" apart.
- Exit codes: `1` means a check failed (report it as a fact); `2` means nothing ran (fix the
  cause: a stale page file, the scaffold placeholder, a refused statement, no connection).

## Done when

The log is committed with every finding cited and verified, every screen mismatch either cited or
dropped by the verifier (or `--no-screen`), check is clean, and the user knows the sign-off is
theirs. With `--offline`: check is clean or its gaps are reported.
