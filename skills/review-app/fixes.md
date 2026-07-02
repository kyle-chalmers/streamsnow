# `--fix` — apply the latest report as atomic commits

Turn the newest `.review/` report (written by a review pass or `/audit-lineage` — same schema) into
per-finding commits, then re-run the gate. This applies findings; it does not re-judge them.

## Buckets

Every finding sorts into exactly one bucket; the bucket decides whether you touch code without asking.

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
7. **Re-gate:** `streamsnow validate-app <slug>` until PASS or the only failures are documented
   deferrals.
8. **Report:** commits applied, decisions made, the punch list, the gate result, and next step
   (/ship-app when clean).

## Focused-check mapping

| Finding | Prove it cleared with |
|---|---|
| Denied/allowed schema swap | `streamsnow check schema-refs apps/<slug>` |
| Egress / code-exec / write-SQL / dynamic SQL | `streamsnow check security apps/<slug>` |
| Missing `@st.cache_data(ttl=…)` | `streamsnow check caching apps/<slug>` |
| `:N IS NULL OR` trap | `streamsnow check bind-predicates apps/<slug>` |
| UI / chart / docs | lint only; verify visually if a Playwright MCP is loaded |

If a "fix" can't be confirmed green by a matching check, it's Bucket B — not an auto-fix.

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
