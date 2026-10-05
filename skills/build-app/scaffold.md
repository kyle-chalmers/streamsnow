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
   local `branding.py`, `sql_loader.py` and `review.py` (the `review_value` marker), the shared
   page modules (`pages/_glossary.py`, `_layout.py`, `_time_controls.py`, `_data.py`), the About
   page (`pages/about.py`, last in the navigation), and
   `sql_review/` (its `index.yaml`, `AGENTS.md`, README and generated page file). **Do not
   hand-create these files**: the scaffold keeps
   them consistent with the governance templates, and `streamsnow update` re-renders the governed
   ones later.

   It also writes a **starter trio that is placeholder content, not a start on the real app**:
   `queries/example_metric.sql` (reads `YOUR_TABLE`), its `example_metric` entry in
   `sql_review/index.yaml` (whose review window reads `YOUR_TABLE` too) and
   `pages/overview.py` (hard-coded sample numbers). They keep the fresh scaffold structurally whole:
   the entrypoint has a page, and `snowflake.yml`'s `pages/` and `queries/` artifacts resolve. The
   build phase replaces all three with the first real page
   ([pages.md § Replace the starter trio](pages.md#replace-the-starter-trio)), and `validate-app`
   FAILS while any app file still reads `YOUR_TABLE`, so a skipped replacement cannot ship. Show
   the three as "starter, replaced in the build phase" when you report the scaffold, never as created pages.
2. If the staged spec lives outside the app dir, `git mv` it to `apps/<slug>/REQUIREMENTS.md` so §11
   travels with the app. Confirm `apps/<slug>/streamlit_app.py` exists before reporting the phase done.
3. **Install the app's packages** into the repo's `.venv`, so the first preview works. `streamsnow
   new` prints the command, matched to the runtime ("Install the app's packages for local
   preview: ..."): an editable install for container apps, the `environment.yml` packages for
   warehouse apps. `/onboard` creates `.venv` (setup §1c), so run only the part after `&&`;
   when `.venv` is missing, create it first with `uv venv --python 3.11`. One line to the user:
   what it installs and why.
4. **Runtime** was decided in the spec (§9) — the scaffold materializes it into `snowflake.yml` and
   the matching manifest. If it's still open, resolve it now via
   [_shared/runtime-decision.md](../_shared/runtime-decision.md); switching after deploy is a
   re-deploy plus a rewrite.
5. If `streamsnow new` says the app already exists, a prior run left a half-scaffolded app — read its
   §11 and resume rather than re-scaffolding. Pass `--force` only when the user explicitly wants to
   overwrite.

## Foundation (after the scaffold, before any page)

The shared layer every page imports, built once by the orchestrator from the CP1b design so the
parallel page-builders don't diverge:

1. **Glossary:** one `Metric` per design `glossary` entry in `pages/_glossary.py`.
2. **Shared data:** for each `shared_data` entry, its `queries/<name>.sql` (with header block)
   and one cached loader in `pages/_data.py` (`@st.cache_data(ttl=...)`, filters as arguments).
   The data's date bounds are usually the first.
3. **About page:** fill `ABOUT` in `pages/about.py` from §1, §2 and §4.
4. Run `streamsnow check caching apps/<slug>` and `streamsnow check page-imports apps/<slug>`,
   then commit the foundation and log §11 (`Next: build`).

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

The first deploy needs one-time Snowflake objects: database, schema, warehouse, CI and viewer
roles, a CI service user, grants, and a stage (or an API integration + secret + git repository).
`streamsnow deploy-setup --admin` emits all of it in `USE ROLE` sections; surface it for the
account owner to review and run once. Never run it yourself. Deploys themselves run
through CI on merge; never run one locally.
