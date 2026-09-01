# `--fix` — apply the latest report as atomic commits

Turn the newest `.review/` report (written by a review pass or `/audit-lineage` — same schema) into
per-finding commits, then re-run the gate. This applies findings; it does not re-judge them.

## Severity × bucket — the canonical taxonomy

Every finding carries two independent labels. **Severity** — `BLOCK` / `FLAG` / `NICE-TO-HAVE` —
is how much it matters: BLOCK = a violated governance rule or confirmed breakage; FLAG = real but
not ship-stopping; NICE-TO-HAVE = polish. **Bucket** — A / B / C — is how the fix gets applied.
The axes are orthogonal: a BLOCK can be Bucket B (matters a lot, needs judgment) and a
NICE-TO-HAVE can be Bucket A (matters little, trivially mechanical). Severity sets ordering and
what the `--auto` loop's exit condition counts; the bucket decides whether you touch code without
asking. Every finding sorts into exactly one bucket.

- **A — mechanical (auto-applied):** the fix is invariant and a regex/AST-level edit suffices.
  Missing `@st.cache_data(ttl=…)`; a denied-schema reference swapped to the allowed equivalent
  *named in the finding*; a write/dynamic-SQL pattern removed; the `:N IS NULL OR` bind-predicate
  trap rewritten; `SELECT *` → named columns **only when the column list is inlined in the finding**
  (only `/audit-lineage` can inline real columns — a static review's `SELECT *` finding defers with
  a TODO note instead); a chart-library *import* swap.
- **B — judgment (walked interactively):** which TTL value, which view to read, a query
  restructure, translating chart specs between libraries, wrapping a page in `st.form`, container
  thread-safety guards. Show the diff, apply only on approval; "skip" and "mark resolved" are valid.
- **C — informational:** context only ("consider materializing upstream") — collect into the
  end-of-run punch list.

**When in doubt between A and B, treat it as B.** A wrong silent auto-fix costs more trust than one
extra question.

## Steps

1. Resolve the slug; confirm `apps/<slug>/streamlit_app.py` exists.
2. **Start clean:** `git status --short apps/<slug>` — atomic commits need a clean tree. Unrelated
   edits → ask the user to stash/commit first; never entangle their work-in-progress.
3. **Load the latest report:** `ls -t apps/<slug>/.review/review-*.md | head -1`. None → run a
   review pass first and stop.
4. **Bucket every finding** and print the plan ("N automatic, N to walk, N heads-up") before any
   commit lands.
5. **Bucket A, one commit per finding, in report order:** apply the edit, re-run the matching
   focused check (table below) plus lint, then
   `git add <files> && git commit -m "fix(<slug>): <summary>"`. A fix that fails its check →
   revert just that edit, mark the finding deferred, keep going. Never batch; never abort the chain
   on one failure.
6. **Bucket B interactively**, committing approved fixes exactly as in step 5.
7. **Record the resolutions:** append them to the report with
   `streamsnow review-loop write-resolutions <report.md> --applied <json> --deferred-b <json>
   --bucket-c <json>` — each `<json>` is a file of finding dicts (the shape
   `streamsnow review-loop parse-findings` emits) or `-` for stdin; an empty group can be omitted.
   This is what the `--auto` loop's dedup and no-convergence detector read: an unrecorded applied
   fix gets re-reported next cycle, and a finding that returns *after* its recorded fix is the
   no-convergence signal instead of silently deduped noise.
8. **Re-gate:** `streamsnow validate-app <slug>` until PASS or the only failures are documented
   deferrals.
9. **Report:** commits applied, decisions made, the punch list, the gate result, and next step
   (/ship-app when clean).

## Focused-check mapping

| Finding | Prove it cleared with |
|---|---|
| Denied/allowed schema swap | `streamsnow check schema-refs apps/<slug>` |
| Egress / code-exec / write-SQL / dynamic SQL | `streamsnow check security apps/<slug>` |
| Missing `@st.cache_data(ttl=…)` | `streamsnow check caching apps/<slug>` |
| `:N IS NULL OR` trap | `streamsnow check bind-predicates apps/<slug>` |
| UI / chart / docs | lint only; verify visually if a Playwright MCP is loaded |
| Any edit to `queries/*.sql` or the data layer | `streamsnow sql-review check <slug>` |

If a "fix" can't be confirmed green by a matching check, it's Bucket B — not an auto-fix.

A fix that touches `queries/*.sql` (or the data modules the token dispatchers call into) makes the
rendered audit trail stale: run `streamsnow sql-review generate <slug>` and then
`streamsnow sql-review check <slug>` **before the commit**, and commit the regenerated
`sql_review/` files with it — otherwise the very next `check` reads DRIFT.

## Runtime-aware fixes

Read the app's runtime before touching deps or connection code
([_shared/runtime-decision.md](../_shared/runtime-decision.md)):
manifest dialect fixes apply the right pin form for that runtime (never sweep the whole file);
deleting a `python` pin from a warehouse manifest is a safe Bucket A fix; adding `ttl=0` to the
inner container connection call applies only in container apps and only when the finding cites it.
Runtime undeterminable → reclassify as Bucket B and ask.

## Guardrails

- **Never `--no-verify`.** A pre-commit hook failure is a real finding; fix the cause.
- **Stale line numbers:** if the cited `file:line` no longer matches, don't force the recipe —
  reclassify as B or re-run the review.
- **Never introduce a denied-schema reference.** If a proposed fix would, refuse and downgrade to C
  with the policy reason.
- **No branch switching, no pushing** — the atomic commits become the `/ship-app` PR body.
