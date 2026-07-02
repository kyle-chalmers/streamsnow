# Scaffold phase — `streamsnow new` plus the first-build conventions

Scaffold `apps/<slug>/` into a governed repo and seed it so the validate gate passes on real
content, not luck. The scaffold writes everything the gate expects; the job here is to fill it with
real pages, queries, and branding without breaking the governance contract.

## Scaffold

1. **Derive `<domain>` / `<function>` from the spec §1** (slug = `<domain>-<function>`). Run:
   ```
   streamsnow new <domain> <function>
   ```
   This writes `apps/<slug>/` — entrypoint, dependency manifest, `snowflake.yml`, app `AGENTS.md`,
   local `branding.py` and `sql_loader.py`. **Do not hand-create these files**: the scaffold keeps
   them consistent with the governance templates, and `streamsnow update` re-renders the governed
   ones later.
2. If the staged spec lives outside the app dir, `git mv` it to `apps/<slug>/REQUIREMENTS.md` so §11
   travels with the app. Confirm `apps/<slug>/streamlit_app.py` exists before reporting the phase done.
3. **Runtime** was decided in the spec (§9) — the scaffold materializes it into `snowflake.yml` and
   the matching manifest. If it's still open, resolve it now via
   [_shared/runtime-decision.md](../_shared/runtime-decision.md); switching after deploy is a
   re-deploy plus a rewrite.
4. If `streamsnow new` says the app already exists, a prior run left a half-scaffolded app — read its
   §11 and resume rather than re-scaffolding. Pass `--force` only when the user explicitly wants to
   overwrite.

## First-build conventions (apply to every page you fill in)

Work inside `apps/<slug>/` only — touching files outside the app dir breaks the governance boundary
the checks enforce, and container apps can't import repo-level shared modules anyway.

- **Pages register in `st.navigation` + `st.Page`** in the entrypoint — never the legacy `pages/`
  auto-discovery. Wire branding through the scaffolded local `branding.py`.
- **Every UI-feeding query lives in `apps/<slug>/queries/<name>.sql`**, loaded through the scaffolded
  `sql_loader` — never inlined as a Python f-string. Each file opens with the required header block
  (`Query / Feeds / Schemas / Params / Tokens`); copy the shape from an existing file. Named-column
  `SELECT`s against allowed schemas only.
- **Cache every data fetch:** `@st.cache_data(ttl=...)` with the repo default TTL unless §8 says
  otherwise. Pass filter values as function arguments, not closures — closures poison the cache key.
- **Optional filters use `{TOKEN}` fragments**, never `(:N IS NULL OR col = :N)` — see the
  bind-predicate note in [spec.md](spec.md).
- **Update the app `AGENTS.md`** with data sources, business logic, and any non-default TTL or
  runtime notes — it's what future sessions and reviewers read first.

## Lint as you go

Run single checks while iterating instead of discovering everything at the gate:

```
streamsnow check schema-refs apps/<slug>      # blocks references to denied schemas
streamsnow check security apps/<slug>         # blocks egress, code-exec, write-SQL, dynamic SQL
streamsnow check caching apps/<slug>          # requires @st.cache_data(ttl=...) on data fetches
streamsnow check bind-predicates apps/<slug>  # blocks the :N IS NULL OR trap
```

Add `--format json` to parse results. These are the same checks `streamsnow validate-app` bundles.

## First app in a fresh Snowflake account

The first deploy needs one-time Snowflake objects (a stage, or an API integration + secret + git
repository, depending on the configured deploy source). `streamsnow deploy-setup` emits that DDL —
surface it for the account owner to review and run once with an admin role. Deploys themselves run
through CI on merge; never run one locally.
