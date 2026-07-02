# The five reviewer dimensions

Each reviewer is a read-only subagent with a self-contained brief (no conversation context), the
detected runtime mode, the load-bearing governance excerpts, a ≤600-word cap, `[file:line]`
citations, and a severity (critical / should-fix / nice-to-have) on every finding.

## SQL efficiency

`SELECT *`, wide-view joins where a narrower governed view exists, unbounded scans, missing filter
pushdown, duplicated CTEs that could factor into a shared file. Sanity-run
`streamsnow check schema-refs apps/<slug>` and `streamsnow check bind-predicates apps/<slug>`.
Flag table choice for human review — never claim warehouse-metadata knowledge; phrase "a narrower
governed view may exist" as a question, not an assertion.

## Data / lineage sanity

Schema allowlist adherence (governed view vs. raw base table), column fidelity, cache-key
correctness (filter params must be function arguments, not closure variables), TTL appropriateness
vs. stated freshness needs, wide unfiltered DataFrames that risk the result-size ceiling. Static
read only — live-DB tracing belongs to `/audit-lineage`.

## UI / Streamlit patterns

`st.set_page_config` first and called once; `st.navigation` + `st.Page` for multipage; branding
applied in the entrypoint; multi-filter pages batched in `st.form` (one rerun, not one per widget);
consistent chart library; number/`column_config` formatting. If a Playwright MCP is loaded, drive a
live walkthrough per [_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md) to
confirm visuals populate under default filters — a whole band of empty visuals under defaults is
**critical**; a single intentional empty-state beside an `st.info`/`st.warning` is fine. Degrade
silently to source-only when the MCP is absent.

## Runtime / config

`snowflake.yml` shape matches the detected mode; connection pattern matches runtime; dependency
manifest pinned in the right dialect (conda vs. PEP 440), warehouse manifests must not pin Python;
thread-safe module state in container (shared server); the declared role is a scoped service role,
not a personal dev role. Run `streamsnow check caching apps/<slug>` for TTL coverage. A declared
runtime that mismatches the connection pattern or the manifest dialect is a real failure, not
cosmetic — but the problem is the *inconsistency*; both runtimes are legitimate
([_shared/runtime-decision.md](../_shared/runtime-decision.md)).

## Docs / governance

App `AGENTS.md` ↔ code drift (tables/pages it claims vs. what exists), `REQUIREMENTS.md` coverage of
what shipped, README present and not a placeholder. Run `streamsnow check security apps/<slug>` for
egress / code-exec / write-SQL / dynamic-SQL.

## Merge rules

Collapse byte-equal `[file:line]` citations to one line tagged `(also flagged by …)`; keep two
different reasons on the same line as separate findings. Each merged line: severity · dimension ·
`[file:line]` · one-sentence fix. Keep critical reasons concrete — a rule violated or a real
breakage, never stylistic opinion.

## Troubleshooting a review run

- **A reviewer returns empty sections** — it misread scope; re-dispatch just that dimension.
- **Review flags something validate-app passed** — expected: this tier covers judgment beyond the
  gate's regex/AST reach.
- **Cross-agent CLI hangs or returns nothing** — that dimension degrades to Claude-only; the recipe
  bounds each external call and never aborts the run.
- **"Nothing to review" in diff mode with real changes** — `origin/main` is stale; fetch per
  [_shared/sync-with-main.md](../_shared/sync-with-main.md) and retry.
