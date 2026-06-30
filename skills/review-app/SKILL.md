---
name: review-app
description: Qualitative deep review of a StreamSnow app — fans out parallel Claude Code subagents across SQL, data, UI, runtime, and docs, then merges findings into BLOCK/FLAG/NICE-TO-HAVE. Use when the user says "review this app", "audit my dashboard", "optimize this app", or after a clean `streamsnow validate-app`.
---

# review-app

Qualitative, judgment-based deep review of `apps/<slug>` via parallel subagent reviewers — surfaces what a senior reviewer would flag, never blocks.

This is the QUALITATIVE tier. `streamsnow validate-app <slug>` is the deterministic PASS/FAIL ship gate; this skill never blocks a ship. Run /validate-app first so reviewers spend tokens on judgment, not on lint the gate already catches.

## When to run

- After /new-app once pages are actually wired up (an empty scaffold has nothing to review).
- After /migrate-app conforms an app, before opening the PR.
- When the user asks to "optimize" or "audit" — depth beyond the gate.
- /ship-app prompts for this if no recent report exists; /auto-review-app runs it on a loop with /apply-review.

## Steps

1. **Resolve the slug.** If given, confirm `apps/<slug>/` exists. Else if cwd is inside an app, use it. Else list `apps/*/` and ask which one. Stop early if `apps/<slug>/streamlit_app.py` is missing — that is not a reviewable app.

2. **Read governance context before dispatch.** Read `streamsnow.config.yaml` (governance.schema_allow / schema_deny, governance.database; caching defaults; snowflake.objects/roles; `review.cross_agent`), the app's `AGENTS.md`, and `REQUIREMENTS.md` if present. Reviewers cite governance, so stale orchestrator understanding propagates into every brief.

3. **Detect runtime mode** from the app's `snowflake.yml`. Container mode declares a `runtime_name` (plus compute pool + external access); warehouse mode declares none of those. Match the pattern on an actual key, not a comment — apps often keep a "flip back to container" note in a comment, and a loose grep mis-reports the runtime, poisoning the runtime/data reviewers. Store the mode; reviewers branch on it.

4. **Run the deterministic gate first:**
   ```
   streamsnow validate-app <slug>
   ```
   Note any FAIL/WARN so reviewers do not re-report what the gate already caught.

5. **Optional diff scope.** For a branch/PR review, compute the changed file set vs the default branch (`git diff --name-only origin/main...HEAD -- apps/<slug>/`). Empty set → "Nothing changed in this app vs main" and stop. Otherwise pass the list to reviewers and tell them to cite only findings inside it (they may still Read other files for context). See `_shared/sync-with-main.md` if `origin/main` is stale.

6. **Fan out 5 parallel reviewers** with the Task tool — one read-only subagent per dimension, single message / multiple Task calls (serial dispatch defeats the purpose). Each gets a self-contained brief (no conversation context), the runtime mode, the load-bearing governance excerpts, a ≤600-word cap, and must cite `[file:line]` and tag every finding BLOCK / FLAG / NICE-TO-HAVE:
   - **SQL efficiency** — `SELECT *`, wide-view joins where a narrower governed view exists, unbounded scans, missing filter pushdown, duplicated CTEs that could factor into a shared file. Have it sanity-run `streamsnow check schema-refs apps/<slug>` and `streamsnow check bind-predicates apps/<slug>`. Flag table choice for human review — do not claim warehouse-metadata knowledge.
   - **Data / lineage sanity** — schema allowlist adherence (governed view vs raw base table), column fidelity, cache-key correctness (filter params must be function arguments, not closure variables), TTL appropriateness vs stated freshness needs, wide unfiltered DataFrames that risk the result-size ceiling. Static read only — live-DB tracing belongs to /deep-dive-data.
   - **UI / Streamlit patterns** — `st.set_page_config` first and called once; `st.navigation` + `st.Page` for multipage; branding applied in the entrypoint; multi-filter pages batched in `st.form` to avoid a rerun per widget; consistent chart library; number/`column_config` formatting. If a Playwright MCP is loaded, drive a live walkthrough per `_shared/playwright-walkthrough.md` to confirm visuals populate under default filters (a `default=[]` multiselect rendering an empty chart/table/KPI band passes every static gate — BLOCK it). Degrade silently to source-only when the MCP is absent.
   - **Runtime / config** — `snowflake.yml` shape matches the detected mode; connection pattern matches runtime (warehouse → active session; container → `st.connection`); dependency manifest pinned in the right dialect for the runtime (conda vs PEP 440), and warehouse manifests must not pin Python; thread-safe module state in container (shared server); the declared role is a scoped service role, not a personal dev role. Run `streamsnow check caching apps/<slug>` for TTL coverage.
   - **Docs / governance** — `AGENTS.md` ↔ code drift (tables/pages it claims vs what exists), `REQUIREMENTS.md` coverage of what shipped, README present and not a placeholder. Run `streamsnow check security apps/<slug>` for egress / code-exec / write-SQL / dynamic-SQL.

7. **Optionally add cross-agent reviewers** per `_shared/cross-agent-review.md` — only when `review.cross_agent: true` in config AND an external CLI passes the recipe's smoke test. OFF by default in OSS; degrade silently when disabled or absent. Honor `--no-cross-agent` to force-skip. Cross-agent is additive, not authoritative: surface divergence, never suppress either side.

8. **Merge findings.** Collapse duplicates; on byte-equal `[file:line]` citations keep one line and tag consensus `(also flagged by …)`; keep two flags on the same line for different reasons as separate lines. Sort into BLOCK / FLAG / NICE-TO-HAVE. Each line: severity · dimension · `[file:line]` · one-sentence fix. Keep BLOCK reasons concrete (a rule violated or a real breakage), not stylistic opinion.

9. **Write the report** to `apps/<slug>/.review/review-<ts>.md` (gitignored) with a header (slug, UTC timestamp, runtime mode, scope, reviewers that produced output) and a top-3 summary. Print a plain-English stdout summary that translates the labels — critical / should-fix / nice-to-have — so the user understands it without opening the file.

10. **Offer the next step.** If there is at least one mechanically-fixable BLOCK/FLAG, point to /apply-review for the atomic-commit fix pass. If the app queries governed objects, surface (do not auto-run) the /deep-dive-data tip for live column/filter fidelity.

## Decision guidance

- **Container vs warehouse runtime** drives several reviewer verdicts, so resolve it first (step 3) and pass it to every agent. Mismatches between the declared runtime and the connection pattern or dependency-manifest dialect are runtime-required failures, not cosmetic — flag them hard. Both modes are legitimate; the BLOCK is the *inconsistency*, not the choice.
- **BLOCK vs FLAG.** BLOCK is reserved for a violated governance rule (denied schema reference, missing required security pattern) or a confirmed breakage (visuals empty under default filters). Everything that is a real issue but not ship-stopping is FLAG. Polish is NICE-TO-HAVE. When unsure between BLOCK and FLAG, downgrade — this tier never blocks the gate, and over-blocking trains users to ignore it.
- **Static vs live.** This skill is static-only by design (the 5-reviewer fanout reads code, it does not run SQL). When a finding hinges on what the live data actually returns — row counts, column existence, filter semantics — emit it as a FLAG and route the user to /deep-dive-data rather than guessing.

## Gotchas / edge cases

- **Comment-based runtime mis-detection.** Anchor the `runtime_name` check on a real YAML key; a loose match on the word in a comment flips the whole review's runtime context.
- **Re-reporting the gate.** If /validate-app already FAILed something, reviewers should reference it, not re-list it — duplicates erode trust in the report.
- **Empty governed-view assumptions.** A reviewer must not assert "a narrower REPORTING view exists" without seeing it; phrase such items as "consider whether a narrower governed view exists" and FLAG for human confirmation.
- **Default-filter empty visuals** are the canonical bug static gates miss: ruff, schema-refs, SQL headers, and /validate-app all pass while a `default=[]` multiselect renders zero rows. Only the live UI walkthrough catches it. Treat a whole band of empty visuals under default filters as a BLOCK; a single intentional empty-state with an adjacent `st.info`/`st.warning` is fine.

## Troubleshooting

- **"Nothing to review" in diff mode with real changes** — `origin/main` is stale. Fetch it (see `_shared/sync-with-main.md`) and retry.
- **A reviewer returns empty sections** — it likely misread scope. Re-dispatch just that dimension to inspect its raw output.
- **Report path collision** — timestamped names collide only inside the same UTC minute; re-run and the next minute resolves it.
- **Review flags something /validate-app passed** — expected, not a contradiction. This tier surfaces judgment issues beyond the gate's regex/AST coverage.
- **Cross-agent CLI hangs or returns nothing** — it degrades that dimension to Claude-only and never aborts the run; the recipe bounds each external call.

## Done when

The merged BLOCK/FLAG/NICE-TO-HAVE report is written under `apps/<slug>/.review/`, the plain-English summary is on stdout, and the user has a clear next step. See also /validate-app (the deterministic gate), /deep-dive-data (live-DB fidelity), /apply-review (mechanical fix pass), and /auto-review-app (review + apply loop).
