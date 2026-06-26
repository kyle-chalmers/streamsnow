---
name: review-app
description: Qualitative deep review of a StreamSnow app — fans out parallel Claude Code subagents across SQL, data, UI, runtime, and docs, then merges findings into BLOCK/FLAG/NICE-TO-HAVE. Use when the user says "review this app", "audit my dashboard", "optimize this app", or after a clean `streamsnow validate-app`.
---

# review-app

Qualitative, judgment-based deep review of `apps/<slug>` via parallel subagent reviewers — surfaces what a senior reviewer would flag, never blocks.

This is the QUALITATIVE tier. `streamsnow validate-app <slug>` is the deterministic PASS/FAIL gate; this skill never blocks a ship. Run /validate-app first, then this for depth.

## Steps

1. **Resolve slug.** Confirm `apps/<slug>/` exists. Read `apps/<slug>/AGENTS.md`, `REQUIREMENTS.md` (if present), and `streamsnow.config.yaml` for caching defaults and runtime conventions.

2. **Run the deterministic gate first** so reviewers spend tokens on judgment, not lint:
   ```
   streamsnow validate-app <slug>
   ```
   Note any FAIL/WARN — reviewers should not re-report what the gate already caught.

3. **Fan out 5 parallel reviewers** with the Task tool — one subagent per dimension, each scoped to `apps/<slug>` and instructed to cite `[file:line]` and tag each finding BLOCK / FLAG / NICE-TO-HAVE:
   - **SQL efficiency** — `SELECT *`, wide-view joins, unbounded scans, missing pushdown; have it sanity-run `streamsnow check schema-refs <slug>`, `check bind-predicates <slug>`.
   - **Data / lineage sanity** — schema allowlist, view-vs-base-table choice, column fidelity, reconciliation gaps. (Static read only — live-DB tracing is /deep-dive-data.)
   - **UI / Streamlit patterns** — page layout blocks, `st.form` batching, chart library, number formatting, branding contract.
   - **Runtime / config** — `snowflake.yml` runtime, deps manifest, connection pattern, thread-safety, `streamsnow check caching <slug>` TTLs.
   - **Docs / governance** — `AGENTS.md` ↔ code drift, `REQUIREMENTS.md` coverage, security via `streamsnow check security <slug>`.

4. **Optionally add cross-agent reviewers** per `_shared/cross-agent-review.md` — only when `agy`/`codex` are on PATH and enabled in config. OFF by default in OSS; degrade silently when absent. Pass `--no-cross-agent` to force-skip.

5. **Merge findings.** Collapse duplicates, mark `(also flagged by …)` consensus, sort into BLOCK / FLAG / NICE-TO-HAVE. Each line: severity · dimension · `[file:line]` · one-sentence fix. Keep BLOCK reasons concrete (rule violated or breakage).

6. **Write the report** to `apps/<slug>/.review/review-<ts>.md` (gitignored) and print the summary. End by offering /apply-review for the mechanical Bucket A fixes.

## Done when

The merged BLOCK/FLAG/NICE-TO-HAVE report is written under `apps/<slug>/.review/` and surfaced to the user, with a pointer to /apply-review. See also /deep-dive-data (live-DB), /validate-app (gate), /auto-review-app (loop).
