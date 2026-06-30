---
name: new-app
description: Scaffold a new Streamlit-in-Snowflake app with `streamsnow new <domain> <function>`, then guide the first build (pages, queries, branding) and hand off to /preview-app and /validate-app. Use when the user says "new app", "scaffold a dashboard", "add an app", or "start a new dashboard".
---

# new-app

Scaffold a new StreamSnow app into an existing governed repo, seed its first real build, and hand off to preview and validate. The scaffold writes everything the deterministic gate expects; your job is to fill it with real pages, queries, and branding without breaking the governance contract.

This is the focused "scaffold + first build" skill. If the user wants the whole pipeline (spec through shipped PR with sign-off checkpoints), use /start-app instead — it calls this skill as one step.

## Before you scaffold

1. **Check prereqs.** Run `streamsnow doctor`. If anything fails (Python, uv, git, Snowflake CLI, streamlit), stop and hand off to /onboard — building on a broken machine wastes a scaffold.
2. **Confirm the repo is governed.** `streamsnow.config.yaml` must exist at the repo root. If it's missing, this isn't a StreamSnow repo yet — run `streamsnow configure` (or `streamsnow init` for a brand-new repo) before continuing. The config defines the schema allowlist (`governance.schema_allow` / `schema_deny`, `governance.database`), the runtime defaults, the warehouse/roles, and the deploy source that every downstream check reads.
3. **Look for a staged spec.** If the user already ran /refine-requirements, there's a `REQUIREMENTS.md` describing the pages, charts, KPIs, filters, source schemas, caching TTLs, and runtime preference. Read it and keep it in context so the queries and pages you generate map to the spec instead of being invented. No spec yet but the app is non-trivial? Offer /refine-requirements first — a one-page contract makes the build far more accurate.

## Scaffold

4. **Settle the name with the user.** An app slug is `<domain>-<function>` (kebab-case), e.g. domain `marketing`, function `campaign-dashboard` → slug `marketing-campaign-dashboard`. Pick something durable; the slug becomes the directory name and is referenced by every downstream skill.
5. **Scaffold:** `streamsnow new <domain> <function>`. This writes `apps/<slug>/` with the entrypoint, `pyproject.toml` / `environment.yml`, `snowflake.yml`, app `AGENTS.md`, local branding, and the SQL loader. **Do not hand-create these files** — the scaffold keeps them consistent with the governance templates, and `streamsnow update` re-renders the governed ones later. If `apps/<slug>/` already exists, `streamsnow new` errors; pass `--force` only when the user explicitly wants to overwrite.

### Runtime: container vs warehouse

The scaffold materializes one runtime. The repo's default lives in `streamsnow.config.yaml` (and the spec's runtime field overrides it). Guidance:

- **Container runtime** (modern default for most new apps): installs dependencies from PyPI via `pyproject.toml`, runs on a compute pool, and uses `st.connection("snowflake").query(...)` — which behaves the same locally and deployed, so local preview catches grant gaps. Needs a compute pool and an external-access integration to exist in Snowflake before the first deploy.
- **Warehouse runtime**: instant cold start, dependencies from the Snowflake Anaconda channel via `environment.yml`, and `get_active_session()` when deployed. Pick it when the app must avoid compute-pool cost or cold-start latency, or when the spec calls for it.

When in doubt, follow the repo default and confirm with the user rather than overriding it.

## Seed the first build

Work inside `apps/<slug>/` only; touching files outside the app dir breaks the governance boundary the checks enforce.

6. **Add the page(s)** the user (or the spec) described to `pages/`, and register each in the entrypoint's `st.navigation`. Wire branding through the scaffolded helpers (`branding.py`) — container apps carry a local copy and cannot import shared modules from outside the app dir.
7. **Externalize every UI-feeding query** into `apps/<slug>/queries/<name>.sql`. Never inline dashboard SQL as an f-string in Python — pages load SQL through the scaffolded loader. Each file needs the required header block (**Query / Feeds / Schemas / Params / Tokens**); a check enforces its presence. Copy the header shape from an existing file in `queries/`. Name the file for what it queries, and write a named-column `SELECT` against schemas in `governance.schema_allow`.
8. **Cache and parameterize correctly.** Wrap each data-fetch function in `@st.cache_data(ttl=...)` (the caching check requires it; honor any non-default TTLs from the spec). Pass filter values as function args, not closures, so the cache key is correct.
9. **Avoid the bind-predicate trap.** Do not write optional predicates as `(:N IS NULL OR col = :N)` — a deployed warehouse NULL-binds every parameter when any one is `None`, silently returning wrong rows. Use `{TOKEN}` SQL fragments rendered conditionally instead. The `bind-predicates` check blocks this pattern.
10. **Update the app `AGENTS.md`** with app-specific context: data sources, business logic, the tables/schemas used, and any non-default cache TTLs or runtime notes. This is what future Claude sessions and reviewers read first.

## Lint and spot-check as you go

11. Catch problems early instead of at the ship gate. Run a single governance check while iterating:
    - `streamsnow check schema-refs apps/<slug>` — blocks references to denied schemas.
    - `streamsnow check security apps/<slug>` — blocks egress, code-exec, write-SQL, dynamic-SQL.
    - `streamsnow check caching apps/<slug>` — requires `@st.cache_data(ttl=...)` on data fetches.
    - `streamsnow check bind-predicates apps/<slug>` — blocks the `:N IS NULL OR` trap.
    Add `--format json` if you want to parse results programmatically.

## Preview, then gate

12. **Preview against live Snowflake:** hand off to /preview-app (or run `streamsnow preview <slug>`) so the user sees it in the browser. preview-app wires up `.streamlit/secrets.toml` from config if it's missing — the scaffold ships only the example file, so a first preview without secrets fails with an auth error.
13. **Gate before any PR:** hand off to /validate-app (or run `streamsnow validate-app <slug>`). This is the deterministic PASS/FAIL ship gate that runs files + schema-refs + security + caching + bind-predicates together. Any FAIL must be fixed before shipping.
14. **Deep review (optional, once there's real content):** /review-app fans out subagents over SQL, data, UI, runtime, and docs for senior-reviewer-grade feedback. Skip it on an empty scaffold — it's only useful after a page or two of real queries.

## First app in a fresh Snowflake account

15. The first deploy into a new account needs one-time Snowflake objects (a stage, or an API integration + secret + git repository, depending on the configured deploy source). `streamsnow deploy-setup` emits that DDL — surface it for the account owner to review and run once with an admin/CI role. **Do not run deploys locally yourself**; deploy is driven by the configured CI workflow via `streamsnow deploy-sql`. Shipping is handled by /ship-app.

## Done when

- `streamsnow new <domain> <function>` has scaffolded `apps/<slug>/`.
- The first page(s) are registered in `st.navigation`, each UI query lives in `queries/*.sql` with a valid header block, branding is wired, and caching/parameterization follow the conventions above.
- Spot checks (or `streamsnow validate-app <slug>`) pass, and the flow has handed off to /preview-app and /validate-app.

## Troubleshooting

- **"not a StreamSnow repo" / no `streamsnow.config.yaml`:** run `streamsnow configure` (existing repo) or `streamsnow init` (new repo) first.
- **`streamsnow new` says the app already exists:** pick a different `<domain>-<function>`, or pass `--force` only if the user wants to overwrite the existing scaffold.
- **schema-refs check fails:** the query references a schema outside `governance.schema_allow`. Point it at an allowed schema, or have the config owner adjust the allowlist — don't work around the check.
- **caching check fails:** a data-fetch function is missing `@st.cache_data(ttl=...)`. Add it; pass filters as args, not closures.
- **Local preview works but deployed returns wrong/empty rows:** suspect the bind-predicate trap (step 9) or a runtime-connection mismatch (`st.connection` vs `get_active_session`).
- **Governance files look stale after a config change:** run `streamsnow update --apply` to re-render `AGENTS.md`, hooks, CI, and deploy templates (it leaves `README` and `.gitignore` alone).

## Related skills and recipes

- /onboard — first-time machine setup when `streamsnow doctor` fails.
- /refine-requirements — write the `REQUIREMENTS.md` spec before scaffolding.
- /add-page — add a further page to an app that already exists.
- /preview-app — run the app locally against live Snowflake.
- /validate-app — the deterministic PASS/FAIL ship gate.
- /review-app — qualitative deep review once there's real content.
- /ship-app — stage, commit, push, and open a PR (gated on validation).
- /start-app — the end-to-end pipeline that orchestrates all of the above.
- /migrate-app — bring an external Streamlit app into the repo instead of scaffolding fresh.
