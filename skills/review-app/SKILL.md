---
name: review-app
description: Review an app the way a senior reviewer would — parallel reviewers across SQL, data, UI, runtime, and docs — then optionally apply the fixes. Use when the user says "review this app", "audit my dashboard", "fix the findings", "auto-fix until clean", or after validation passes. Flags — --fix applies findings as atomic commits, --auto loops review→fix until clean, --sql writes the paste-and-runnable SQL companions.
---

# /review-app

Judgment-tier review of `apps/<slug>` — it surfaces what a senior reviewer would flag and (with
`--fix`) turns findings into atomic per-finding commits. It never blocks a ship; the pass/fail gate
is `streamsnow validate-app` (/validate-app), run first so reviewers spend judgment on what the gate
can't catch.

## Modes

- **Default** — one review pass. Report only; offer `--fix` next.
- **`--fix`** — apply the latest report: mechanical findings auto-commit one-by-one, judgment calls
  are walked interactively. Follow [fixes.md](fixes.md).
- **`--auto`** — loop review → fix → re-review until no new mechanical findings remain, then a
  render smoke. Follow [auto-loop.md](auto-loop.md). Warn it takes minutes (and Snowflake credits
  when the lineage pass joins); `--no-lineage` keeps it static-only.
- **`--sql`** — just write the paste-and-runnable `sql_review/` companions + lineage README for
  audit. Follow [sql-companions.md](sql-companions.md). Read-only; runs automatically when a review
  detects a `sql_review/` gap.

## Review pass

1. **Resolve the slug** (ask, or infer from cwd). Stop early if `apps/<slug>/streamlit_app.py`
   is missing — that's not a reviewable app.
2. **Read context before dispatch:** `streamsnow.config.yaml` (governance lists, caching defaults,
   `review.cross_agent`), the app's `AGENTS.md`, and `REQUIREMENTS.md`. If config is missing, say
   so and continue with what the code alone can show — governance findings just go unverified.
3. **Detect the runtime** — anchored `runtime_name:` key in `snowflake.yml`, never a comment grep
   (see [_shared/runtime-decision.md](../_shared/runtime-decision.md)). Reviewers branch on it.
4. **Run the gate first:** `streamsnow validate-app <slug>` — reviewers must not re-report what it
   already caught.
5. **Optional diff scope:** for a branch/PR review, pass the changed-file list
   (`git diff --name-only origin/main...HEAD -- apps/<slug>/`) and have reviewers cite only inside
   it. Empty diff → say so and stop. Stale `origin/main` → [_shared/sync-with-main.md](../_shared/sync-with-main.md).
6. **Fan out the 5 reviewers in parallel** (single message, multiple Task calls) — SQL, data, UI,
   runtime, docs — each with a self-contained brief per [dimensions.md](dimensions.md), the runtime
   mode, governance excerpts, a ≤600-word cap, `[file:line]` citations, and a severity on every
   finding. Optional cross-agent reviewers ride along only when configured — see
   [_shared/cross-agent-review.md](../_shared/cross-agent-review.md).
7. **Merge:** collapse duplicate citations (`also flagged by …`), sort by severity. Severity means:
   **critical** (BLOCK) — a violated governance rule or confirmed breakage; **should-fix** (FLAG) —
   real but not ship-stopping; **nice-to-have** — polish. When unsure, downgrade — over-blocking
   trains users to ignore the review.
8. **Write the report** to `apps/<slug>/.review/review-<ts>.md` (gitignored) with slug, timestamp,
   runtime, scope, and a top-3 summary. Print a plain-English stdout summary — critical /
   should-fix / nice-to-have counts and the top items — so nobody has to open the file to know
   where they stand.
9. **Offer the next step:** mechanically fixable findings → `--fix`; findings that hinge on live
   data (row counts, real columns, filter semantics) → `/audit-lineage <slug>` rather than guessing.

## Boundaries

- **Static by design.** The review pass reads code; it doesn't run SQL. Live-DB truth is
  `/audit-lineage`.
- **Never weaken governance to clear a finding**, and never re-judge the gate — a review finding
  can't flip validate-app.
- The canonical static-gate escape: a `default=[]` multiselect rendering a whole band of empty
  visuals passes every check — only a live walkthrough catches it
  ([_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md), degrade silently
  without the MCP). One intentional empty-state beside an `st.info` is fine.

## Done when

The merged report is written under `.review/`, the plain-English summary is printed, and the user
has a clear next step (`--fix`, `/audit-lineage`, or ship via /validate-app → /ship-app).
