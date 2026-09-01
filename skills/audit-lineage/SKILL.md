---
name: audit-lineage
description: Check an app's numbers against the live warehouse — trace every Snowflake object it queries, verify the columns it expects actually exist, and map upstream lineage, using bounded read-only queries. Use when the user says "trace the data", "audit the lineage", "are these numbers right", "lineage for <slug>", or when a review finding hinges on what the live data returns.
argument-hint: "<slug>"
allowed-tools: [Bash, Read, Glob, Grep]
---

# /audit-lineage

Confirm the live Snowflake objects an app queries actually match what the code assumes — columns,
lineage, filtering, cost. This is the live-DB tier of review: it sees what static review can't
(real column sets, view definitions, predicate pushdown). It is judgment, never a gate —
`streamsnow validate-app` alone passes or fails a ship. Pairs with `/review-app` (the static
sibling); its findings feed `/review-app --fix` unchanged.

## Preflight

1. **Resolve the slug**; read the app's `AGENTS.md` and `REQUIREMENTS.md` for data conventions and
   the runtime ([_shared/runtime-decision.md](../_shared/runtime-decision.md) — materialization
   advice differs by runtime).
2. **Confirm a live connection** (`streamsnow doctor`, or `snow connection list` against
   `snowflake.connection_name` in config). Without one this skill has no evidence — don't fabricate;
   offer the static path (`/review-app`) instead, and name the enabler
   (`streamsnow configure` + `snow connection add`).

## Discovery

3. **Build the object → expected-columns map** from `queries/*.sql` (headers name upstream objects,
   consuming pages, and params) and any `FROM`/`JOIN` in `.py`. An unusually wide object set is
   itself a should-fix architectural smell. Inline plumbing SQL (`INFORMATION_SCHEMA` lookups in
   `.py`) is infrastructure, not a data surface — log, don't trace.
4. **Governance-gate before touching the DB:** `streamsnow check schema-refs apps/<slug>`. Anything
   in `governance.schema_deny` is **critical** — report it and do NOT query it live. Trace only
   allowed objects.

## Per-object analysis

5. Follow [tracing.md](tracing.md) for each object: surface fidelity
   (`INFORMATION_SCHEMA.COLUMNS` diffed against what the app selects/filters/joins), lineage
   (`GET_DDL`, bounded 2–3 levels up), and filtering/cost signals (pruning traps, `SELECT *`
   passthroughs, materialization candidates — proposals, never applied DDL).

## Read-only discipline (non-negotiable)

6. Every live query is read-only and bounded: `SELECT` / `GET_DDL` / `INFORMATION_SCHEMA` only, no
   DDL or DML ever, `LIMIT` on any row-returning probe, `WHERE 1=0` or `COUNT(*)` to confirm an
   object resolves. Never widen past the allowlist. These probes hit metadata, not data — if you
   want a large scan, stop and reframe the question.

## Output

7. Emit findings in the same report schema as `/review-app` (severity-prefixed bullets, `file` or
   object citations, one-line fix each) so `--fix` and `--auto` consume them unchanged. On a
   `SELECT *` finding, inline the real column list from step 5 — that's what turns it into a
   mechanical fix. Write to `apps/<slug>/.review/` (gitignored); print a plain-English summary.

Severity: **critical** — a mismatch that produces wrong numbers or a runtime error (a
selected/joined column the object doesn't emit, a missing governance-required filter, any denied
object); **should-fix** — measurable cost or coordination risk (view chain ≥3 deep, ignored
soft-delete flag, pruning trap, shared-materialization opportunity); **nice-to-have** — hygiene
notes.

## Audit-trail hand-back

8. **While the lineage results are warm**, run `streamsnow sql-review check <slug>`; on gaps offer
   `discover --write` + `generate` (inside `/review-app --auto`, bootstrap silently with the static
   skeleton defaults). Then `streamsnow sql-review index <slug>` and fill the README's Upstream
   cells from step 5, setting Verified (date) only on live-confirmed rows — see [tracing.md](tracing.md).

## Judgment guardrails

- **Don't claim upstream is broken.** An off-looking metric is far more often a cadence or
  definition mismatch than a corrupt source — frame it that way; assert breakage only with cited
  evidence.
- **`INFORMATION_SCHEMA` is role-scoped:** distinguish "doesn't exist" from "not visible to this
  role" (resolve-probe first) before calling anything critical.
- Optional cross-agent reviewers per [_shared/cross-agent-review.md](../_shared/cross-agent-review.md),
  only when configured; degrade silently.

## Done when

Every allowed object the app queries is traced and column-verified against live Snowflake, denied
references are reported without being queried, and the findings sit in `.review/` in the schema
`/review-app --fix` consumes.
