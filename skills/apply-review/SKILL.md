---
name: apply-review
description: Apply the latest /review-app or /deep-dive-data findings to a StreamSnow app — auto-fix mechanical BLOCK/FLAG findings as atomic per-finding commits and walk judgment-required + NICE items interactively. Use when the user says "apply the review", "fix the findings", "auto-fix review", or after /review-app returns BLOCK/FLAG findings.
---

# apply-review

Turn the latest review findings for an app into atomic per-finding commits, then re-run the deterministic gate.

> Reviews are **qualitative judgment** — they never block. The hard PASS/FAIL gate is `streamsnow validate-app`, run at the end (step 6) and via /validate-app. This skill applies findings; it does not re-judge them.

## Buckets

- **A — mechanical:** unambiguous, auto-applied without asking. Missing `@st.cache_data(ttl=…)`; `SELECT *` → named columns; denied-schema swap to an allowed `REPORTING`/`ANALYTICS` equivalent; missing `use_container_width=True`; Altair → Plotly; bare filter widgets → `st.form`.
- **B — judgment:** needs a human call (TTL value, view choice, query rewrite, UX). Walked interactively.
- **C — informational:** no code change; collated into a punch list.

## Steps

1. Resolve the `<slug>` (ask if absent); confirm `apps/<slug>/` exists. Start clean: `git status --short apps/<slug>` — stash or surface unrelated edits.
2. Load the latest findings report under `apps/<slug>/.review/` (the artifact /review-app and /deep-dive-data write — same Markdown schema from both). If none exists, tell the user to run /review-app or /deep-dive-data first and stop.
3. Bucket every BLOCK/FLAG/NICE finding into A / B / C per the table above. Cite each finding's file + line.
4. **Bucket A — auto-apply, one commit per finding.** Apply the edit, then commit just that finding: `git add <path> && git commit -m "fix(<slug>): <finding summary>"`. Keep commits atomic — one finding, one commit. Re-run the matching focused gate (`streamsnow check schema-refs|security|caching|bind-predicates apps/<slug>`) when the fix targets that gate, to confirm it cleared before committing.
5. **Bucket B — walk interactively.** Present each with the cited line and a proposed fix; apply only what the user approves, committing each atomically as in step 4.
6. **Re-gate.** Run `streamsnow validate-app <slug>` (or hand to /validate-app). Any FAIL → fix and re-run until PASS or a finding needs a human call.
7. **Report.** List Bucket A commits applied, Bucket B decisions, and Bucket C as a punch list. Note the validate result.

## Notes

- Cross-agent (agy/codex) and Jira findings are **optional, off by default** in OSS — apply them only if present in the report and the tools/config are available; otherwise ignore silently. Never create or update tickets unless the user asks.
- Don't re-run the review here — that's /review-app's job. To loop review→fix→review automatically, use /auto-review-app instead.

## Done when

Every Bucket A finding is an atomic commit, Bucket B is resolved or explicitly deferred, Bucket C is reported, and `streamsnow validate-app <slug>` passes (or the only failures are documented human calls).
