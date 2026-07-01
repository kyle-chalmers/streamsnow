---
name: refine-requirements
description: Build or refine an app's REQUIREMENTS.md spec (pages, sections, charts, KPIs, filters, source schemas, caching TTLs, runtime, deploy source) from a free-form description, a ticket, screenshots, or existing code — before scaffolding. Use whenever the user says "spec out a dashboard", "refine requirements", "I have a ticket for an app", "write a REQUIREMENTS doc", "backfill requirements", or wants a contract before /new-app.
---

# refine-requirements

Turn an idea, a ticket, screenshots, or existing app code into a structured `REQUIREMENTS.md` — the durable contract that `/new-app`, `/add-page`, `/validate-app`, and `/review-app` build and audit against. This skill captures *intent*; it does not scaffold, run discovery, or write `queries/*.sql`. Stop at the spec and let the human review before `/new-app`.

Without a spec, the dashboard gets invented mid-scaffold and `/review-app` has nothing to compare the build against. One page of contract makes the whole pipeline far more accurate.

## Modes

Detect the mode from the input, then drive only the gaps:

- **new** — a free-form description or `--new`. Run the interview from a blank slate.
- **ingest** — a ticket reference or pasted ticket body. Extract everything the ticket already answers, then interview only for what it leaves open. If the ticket is sparse (a title and a sentence), don't write a near-empty doc — switch to a fuller interview.
- **backfill** — an existing `apps/<slug>/`. Reverse-engineer the spec from the live source: `streamlit_app.py`, `pages/*.py`, each `queries/*.sql` header block (**Query / Feeds / Schemas / Params / Tokens**), `snowflake.yml`, and the app `AGENTS.md`. The header blocks already name the source schemas and params, so §3/§7 mostly fill themselves; confirm rather than re-ask.

If you can't tell which mode applies, ask once: is this a brand-new app, a ticket to ingest, or an existing app to document?

## Steps

1. **Read the governance config first.** Open `streamsnow.config.yaml` at the repo root for the source-schema allowlist (`governance.schema_allow` / `governance.schema_deny`, `governance.database`), the default runtime, the warehouse/roles, and the deploy source. Everything you write must fit this config — there is no point speccing a schema the checks will block at validate time.
2. **Settle the slug.** The app is `<domain>-<function>` in kebab-case (e.g. domain `reporting`, function `revenue-overview` → slug `reporting-revenue-overview`). The slug becomes the directory name and is referenced by every downstream skill, so pick something durable.
3. **Ingest any visual references before interviewing.** Screenshots, a sketch, a competitor dashboard, or a whiteboard photo — pasted in chat or dropped under `apps/<slug>/screenshots/`. Vision pre-populates the *visual* half of the spec (pages, charts, KPI cards, filter widgets, layout, branding cues), cutting the interview from "describe every chart" to "did I read this right?". Vision can read chart types, KPI labels, and column names off axes — but it cannot tell which table backs each chart or what the cache TTL should be, so the data half still needs an ask. Present a one-screen summary of what you extracted and let the user confirm, correct, or discard it. If there are no screenshots, skip this entirely — do not prompt for them.
4. **Interview for the gaps, one fork at a time.** If the user already gave a paragraph covering several sections, extract what's there and ask only for what's missing; don't quiz section by section. Use a real question only at genuine forks (runtime choice, a non-default cache TTL, an ambiguous page boundary); for everything else propose a sensible default and ask the user to confirm or redirect.
5. **Resolve source schemas against the allowlist.** Every source object in §3 must live under a schema in `governance.schema_allow` (within `governance.database`); a schema on `schema_deny` is banned outright. If the user can't name exact objects yet, that's fine — capture the *data domain* (e.g. "monthly active users", "order line items") so `/new-app` can run `INFORMATION_SCHEMA` discovery during SQL authoring. Do not invent table names.
6. **Decide the runtime.** Default to the repo's configured runtime and only override with a reason recorded in §9:
   - **Container** (modern default): deps from PyPI, runs on a compute pool, queries via `st.connection("snowflake").query(...)` so local preview behaves like deploy. Needs a compute pool + external-access integration in Snowflake before first deploy.
   - **Warehouse**: instant cold start, deps from the Snowflake Anaconda channel, `get_active_session()` when deployed. Choose it when cold-start latency or compute-pool cost rules out container, or when the app has cross-viewer module-level mutable state that's risky on container's shared-process model.
7. **Set caching TTLs.** Use the repo default TTL for every query unless the user justifies a deviation; record any non-default TTL with its reason in §8 (e.g. "near-real-time queue depth → 60s"). Note the upstream refresh cadence so the TTL makes sense against it.
8. **Write `apps/<slug>/REQUIREMENTS.md`** using the section order below. Skip nothing — if a section truly doesn't apply, write `_None_` so reviewers see a deliberate decision, not an oversight.
9. **Echo a one-screen summary** (pages → sections → source objects → TTLs → runtime) and confirm with the user before handing off.

## Required sections

Use this order. §11 is persistent build state that travels with the app so any later session can resume mid-build.

```markdown
# <App Name> — Requirements

**Source:** <ticket ref or "Local-only">
**Status:** Draft
**Last updated:** <YYYY-MM-DD>

## 1. Identity
- **domain / function / slug:** <domain> / <function> / <domain>-<function>
- **Description:** <1–2 sentences — what question this dashboard answers>

## 2. Audience & Use
- **Primary audience / cadence / decision driven:** <role> / <daily|weekly|on-demand> / <action a viewer takes>

## 3. Source Schemas
Only schemas on `governance.schema_allow` (within `governance.database`) are allowed; `schema_deny` is banned.
- <DATABASE.SCHEMA.OBJECT — what it provides>  (e.g. <database>.ANALYTICS.* / <database>.REPORTING.*)
- _If unknown:_ name the data domain so /new-app can run INFORMATION_SCHEMA discovery.

## 4. Pages & Sections
- **<Page 1>** (`pages/<page1>.py`) — <Section> — <chart/KPI/table>
Register each page in `st.navigation` + `st.Page` (not the legacy `pages/` auto-discovery).

## 5. Charts
| Name | Type | X / Dimension | Y / Measure | Group-by | Expected rows |

## 6. KPIs
| Name | Formula | Format ($/%/count) | Comparison delta |

## 7. Filters
| Name | Scope (global/page) | Type (date range/multi/single) | Default |

## 8. Caching & Refresh
- **Default TTL:** <repo default>s · **Non-default:** <query — TTL — why> · **Upstream cadence:** <e.g. hourly dynamic table>

## 9. Runtime
- **Choice:** container | warehouse · **Justification (if not the repo default):** <reason>

## 10. Open Questions
- <unresolved question>

## 11. Build Progress
**Current phase:** spec · **Last action:** /refine-requirements completed
### Pages
| Page | File | Status | Notes |
| <Page from §4> | `pages/<file>.py` | pending | not yet scaffolded |
### Sessions
- <YYYY-MM-DDTHH:MMZ> — /refine-requirements wrote this spec
### Blockers
- _none_
### Resume hint
Next: `streamsnow new <domain> <function>` (or /new-app). Any later session should read §11 first — it reflects live build state, not just the spec.
```

§11 is what `/start-app` reads to decide where to pick up, and what `/add-page`, `/apply-review`, `/preview-app`, and `/ship-app` append a Sessions row to as they make progress. `/review-app` ignores §11 — it audits the spec, not the build log.

## Gotchas & edge cases

- **Sparse ticket.** A one-line ticket is a prompt, not a spec. Run the full interview; don't transcribe the ticket into a hollow doc.
- **`apps/<slug>/REQUIREMENTS.md` already exists.** Ask before overwriting, and offer to diff so the user sees what changes. Pass `--overwrite` only when the user explicitly wants to clobber.
- **Slug isn't an existing app (backfill expected).** If the user passed a slug but `apps/<slug>/` doesn't exist, it's not a backfill — ask whether they meant a new spec, and run `ls apps/` to show what exists.
- **Schema not on the allowlist.** If the user names an object under a `schema_deny` schema, flag it now. The `schema-refs` check would block it at validate time anyway — better to redirect to an allowed reporting view in the spec than discover it after scaffold.
- **Vision over-reads.** Two screenshots may be one page (tabs) or two separate pages. Capture the ambiguity in §10 rather than guessing the page count.
- **Bind-predicate trap, recorded early.** If a filter is optional, note in §7 that it renders as a conditional `{TOKEN}` SQL fragment, never `(:N IS NULL OR col = :N)` — a deployed warehouse NULL-binds every param when one is `None`, silently returning wrong rows. The `bind-predicates` check blocks the bad pattern; speccing it correctly avoids a rebuild.

## Troubleshooting

- **No `streamsnow.config.yaml` at the repo root.** This isn't a governed repo yet — you can't resolve the allowlist or runtime. Hand off to `streamsnow init` (new repo) or `streamsnow configure` (existing repo) before speccing.
- **User can't name a single source table.** Acceptable. Record the data domain in §3 and leave object discovery to `/new-app`. Don't block on it.
- **Want to validate the spec captured the data side?** It can't be validated until code exists. After `/new-app` + `/add-page`, run `streamsnow validate-app <slug>` for the deterministic gate, then `/review-app <slug>` to audit the build against this doc.

## Done when

- `apps/<slug>/REQUIREMENTS.md` exists with all 11 sections (`_None_` where a section doesn't apply).
- Every source object in §3 resolves to a schema on `governance.schema_allow` within `governance.database` — none on `schema_deny`.
- §9 records the runtime, with a justification if it deviates from the repo default.
- §11 records `phase: spec` and a resume hint.
- The user has confirmed the one-screen summary.

## Hand off

- **New app:** `streamsnow new <domain> <function>` (or `/new-app`), then `/add-page <slug> <page>` per page in §4. For the full spec→PR pipeline with checkpoints, use `/start-app`.
- **Existing app (backfill):** `streamsnow validate-app <slug>`, then `/review-app <slug>` to audit the live build against the freshly written spec.
- Need to confirm what data actually exists before committing to §3 objects? `/deep-dive-data` explores the warehouse; bring its findings back here to firm up the spec.
