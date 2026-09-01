# Spec phase — write or refresh `apps/<slug>/REQUIREMENTS.md`

Turn an idea, a ticket, screenshots, or existing app code into the structured spec that the
scaffold, build, validate, and review phases all work against. This phase captures *intent*; it does
not scaffold or write `queries/*.sql`. Stop at the spec and let the human review it.

## Pick the mode from the input

- **new** — a free-form description or nothing yet. Interview from a blank slate.
- **ingest** — a ticket reference or pasted ticket body. Extract everything the ticket answers,
  interview only for what it leaves open. A one-line ticket is a prompt, not a spec — run the full
  interview rather than transcribing it into a hollow doc.
- **backfill** — `apps/<slug>/` already has source code. Reverse-engineer the spec from it
  (see "Backfill" below) — automatic; don't interview for what the code already answers.

If the mode is genuinely ambiguous, ask once: brand-new app, ticket to ingest, or existing app to
document?

## Steps (new / ingest)

1. **Read the governance config first.** `streamsnow.config.yaml` gives the schema allowlist
   (`governance.schema_allow` / `schema_deny`, `governance.database`), default runtime, and deploy
   source. Everything you write must fit it. If the repo isn't configured yet, keep going but mark
   §3 unverified and point at `/start-app --setup`.
2. **Settle the slug** — `<domain>-<function>`, kebab-case, durable (it becomes the directory name).
3. **Ingest visual references before interviewing.** Screenshots or sketches pre-populate the visual
   half (pages, charts, KPI cards, filters, layout); confirm the read with the user in one screen.
   Vision can't tell which table backs a chart or what the cache TTL should be — still ask for the
   data half. No screenshots → skip silently; don't prompt for them.
4. **Interview only for the gaps.** Extract what the user already gave; ask real questions only at
   genuine forks (runtime, a non-default TTL, an ambiguous page boundary). Otherwise propose a
   default and ask to confirm or redirect.
5. **Resolve source schemas against the allowlist.** Every §3 object must live under an allowed
   schema. If the user can't name exact objects, capture the *data domain* ("order line items") for
   discovery during SQL authoring — never invent table names.
6. **Decide the runtime** per [_shared/runtime-decision.md](../_shared/runtime-decision.md); default
   to the repo's configured runtime, record any deviation with its reason in §9.
7. **Set caching TTLs.** Repo default unless justified; record non-defaults with reasons in §8,
   noting the upstream refresh cadence.
8. **Write the spec** with the section schema below, then **echo a one-screen summary**
   (pages → sections → source objects → TTLs → runtime) and confirm before handing back.

## Backfill — reverse-engineer the spec from existing source

Read-only analysis; write the same schema. What feeds what:

| Source | Feeds | How |
|---|---|---|
| `streamlit_app.py` | §4 pages | parse `st.Page(...)` entries and `st.navigation` groups |
| `pages/*.py` | §5 charts, §6 KPIs, §7 filters | chart calls' `x=`/`y=`/`color=` kwargs; `st.metric`/branded-metric calls; widget types + defaults (sidebar/shared → global scope, in-page → page scope) |
| `queries/*.sql` headers | §3 schemas, §4 wiring | the `Query / Feeds / Schemas / Params / Tokens` block names upstream objects and consuming pages |
| `@st.cache_data(ttl=…)` | §8 caching | flag non-default TTLs as explicit rows |
| `snowflake.yml` + manifest | §9 runtime | anchored `runtime_name:` key — never a comment grep |
| app `AGENTS.md` / `README` | §1–§2 | description, audience |

Rules: infer KPI formulas by tracing the value in scope (`df["X"].sum()` → `SUM(X)`,
`len(df)` → `COUNT(*)`); mark anything not confidently extracted **`(inferred)`** and list those in
§10 for human confirmation; write `_None_` rather than fabricating; missing SQL headers are
themselves a §10 finding. Set §11 to `Current phase: in-production (backfilled)`. Do **not**
auto-commit — the user reviews the `(inferred)` items first.

If `REQUIREMENTS.md` already exists (any mode), ask before overwriting and offer a diff.

## Section schema

```markdown
# <App Name> — Requirements
**Source:** <ticket ref | "Local-only" | "Backfilled from source on YYYY-MM-DD">
**Status:** Draft · **Last updated:** <YYYY-MM-DD>

## 1. Identity        — domain / function / slug + a 1–2 sentence description
## 2. Audience & Use  — who reads it, how often, what decision it drives
## 3. Source Schemas  — allowed-schema objects (or the data domain if unknown)
## 4. Pages & Sections — one bullet per page (`pages/<file>.py`) with its sections/visuals
## 5. Charts          — | Name | Type | X | Y | Group-by | Expected rows |
## 6. KPIs            — | Name | Formula | Format | Comparison delta |
## 7. Filters         — | Name | Scope | Type | Default |
## 8. Caching & Refresh — default TTL · non-default TTLs with reasons · upstream cadence
## 9. Runtime         — container | warehouse · justification if not the repo default
## 10. Open Questions — unresolved items, every `(inferred)` marker
## 11. Build Progress
**Current phase:** spec
### Sessions
- <YYYY-MM-DDTHH:MMZ> — spec written (/start-app). Next: scaffold (`/start-app <slug>`).
```

Write every section — `_None_` where one truly doesn't apply, so reviewers see a decision, not an
oversight. §11 is the resume contract: a phase line plus an append-only session log whose last line
names the next command. There is no per-page status table — page state is visible in the tree and
git; the log records what happened and what's next.

`streamsnow check requirements apps/<slug>` validates exactly this contract — the section exists,
the phase is a recognized lifecycle value, the last session line carries an ISO timestamp and (for
a non-terminal phase) a `Next:` hint. It runs inside `streamsnow validate-app`, so a hand-mangled
§11 becomes a named finding instead of a silent failure to resume. Run it after any hand edit to
the section.

## Gotchas

- **Optional filters, recorded early:** an optional filter renders as a conditional `{TOKEN}` SQL
  fragment, never `(:N IS NULL OR col = :N)` — the deployed driver NULL-binds every parameter when
  one is `None`, silently returning wrong rows. Note it in §7 now to avoid a rebuild.
- **Two screenshots may be one page (tabs).** Capture page-count ambiguity in §10, don't guess.
- **A denied schema in §3** will be blocked at validate time anyway — redirect to an allowed
  reporting view now.
