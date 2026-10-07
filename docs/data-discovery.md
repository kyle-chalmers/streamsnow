# Data discovery

How to find tables and columns in Snowflake when you're building a dashboard,
and how to wire queries so they pass StreamSnow's governance checks. Table names
are **not** hardcoded anywhere — `INFORMATION_SCHEMA` is the source of truth.

Throughout, the schemas are your `governance.sources` from `streamsnow.config.yaml`
(`DATABASE.SCHEMA` entries, possibly in several databases).

## The two queries you need

**List the tables/views your apps are allowed to query:**

```sql
SELECT table_schema, table_name, table_type, comment
FROM <database>.INFORMATION_SCHEMA.TABLES   -- one source's schemas; repeat per source database
WHERE table_schema IN ('REPORTING')
ORDER BY table_schema, table_name;
```

**Inspect the columns of a specific table:**

```sql
SELECT column_name, data_type, is_nullable, comment
FROM <database>.INFORMATION_SCHEMA.COLUMNS
WHERE table_schema = 'ANALYTICS' AND table_name = '<TABLE_NAME>'
ORDER BY ordinal_position;
```

Run these in Snowsight, via `snow sql`, or in a scratch page during preview.
Results are filtered by your current role's privileges — if a table you expect
doesn't appear, it's usually a role gap (see [below](#when-your-role-cant-see-a-table)).

## How governance shapes what you can query

The boundary is config, checked by `streamsnow check schema-refs` in pre-commit,
`validate-app` and CI, and by the live SQL review before anything is sent:

- **`sources`**: the `DATABASE.SCHEMA` locations your apps read, in any databases.
  `deploy-setup --admin` grants the CI role (which runs every deployed app) read on
  exactly these. Name objects in full: `DATABASE.SCHEMA.OBJECT`.
- **`app_data`**: the one schema per repo for views and dynamic tables built for the
  apps. Query sources directly unless an object there earns its place.
- **`schema_deny`**: schemas that are blocked outright (raw, landing, bridge layers).
  `RAW` blocks a `RAW` schema in every database; `FINANCE.RAW` only in `FINANCE`.
  A denied reference always fails.
- **`read_exceptions`**: exact `DB.SCHEMA.OBJECT` names readable despite the deny
  list; the rest of that schema stays blocked.
- **`boundary`**: what happens to a name outside `sources` and `app_data`, or a
  two-part `SCHEMA.OBJECT` name (it resolves against the session's database, which
  differs between preview and the deployed app). `warn` (the default) reports it;
  `enforce` fails it. A two-part name (for example after `USE DATABASE`) is only a
  warning under `boundary: warn`, even when its schema matches a qualified deny entry,
  so app SQL should use `DATABASE.SCHEMA.OBJECT`. Only names in relation position count
  (after `FROM`, `JOIN`, `INTO`, `UPDATE`, the literal inside `IDENTIFIER('...')` or
  `TABLE('...')`, after `USE`): `t.id` or `params.start_date` are columns, never
  objects. Quoted names keep their case, as in Snowflake: `"analytics_db"."reporting"`
  is not the source `ANALYTICS_DB.REPORTING`. `INFORMATION_SCHEMA` and `SNOWFLAKE.*`
  are ignored.
- **`imported_databases`**: shares that hold sources. They take `IMPORTED PRIVILEGES`
  instead of per-schema grants (`SNOWFLAKE` and `SNOWFLAKE_SAMPLE_DATA` are built in).

`streamsnow configure` and `streamsnow doctor --live` check, read-only, that each
source is visible to your connection's role. That proves visibility, not SELECT, and
it is your role, not the CI role.

## Rules of thumb

- **Prefer a curated/reporting layer over raw tables.** Reporting-style tables
  are narrow and pre-aggregated for dashboard query shapes; raw/analytics views
  are wide and heavy. Don't join three wide views for three columns.
- **Explicit column lists, never `SELECT *`.** Schema changes break `SELECT *`
  silently; explicit lists fail loudly. (This is a habit StreamSnow's checks
  don't enforce — adopt it anyway.)
- **Filter in SQL, not in Python.** The warehouse runtime caps a message at
  32 MB and the container runtime at 200 MB by default
  ([limitations](https://docs.snowflake.com/en/developer-guide/streamlit/limitations)) —
  push filters into `WHERE` so the returned DataFrame stays small on either.
- **Cache every loader, and key it on its filters.** Decorate data loaders with
  `@st.cache_data(ttl=...)` and pass filter values as function arguments so the
  cache key reflects them. `streamsnow check caching` requires a TTL on public
  loaders (including ones that reach the query through a local variable or a
  private helper).

## Wiring a query into an app

Scaffolded apps keep SQL out of Python:

1. Put the statement in `apps/<slug>/queries/<name>.sql` (one named query per
   file).
2. Load it with the scaffolded helpers in `sql_loader.py` — `load_sql("<name>")`
   for the raw text, or `render_sql("<name>", TOKEN=value)` to substitute
   `{UPPERCASE_TOKEN}` placeholders (it uses `str.replace`, not `str.format`, so
   SQL like `{2}` doesn't collide).
3. Pass user-supplied values as **bind parameters**, never string-formatted into
   the SQL — `conn.query(sql, params=[start, end])`. `check security` flags
   dynamic SQL, and `check bind-predicates` flags bind traps that the deployed Go
   driver mishandles.
4. Wrap the loader in `@st.cache_data(ttl=...)`.

## When your role can't see a table

Some tables are restricted by role. If `INFORMATION_SCHEMA.TABLES` doesn't show
what you expect:

1. **Check the role.** Deployed apps read with the CI role's grants (owner's
   rights), so preview with a role whose reads match the CI role's: the viewer
   role with the opt-in data grants from `deploy-setup --admin` uncommented, or a
   developer role with the same reads. A query that works under a broad personal
   role but not under the CI role's grants will ship as empty or erroring data.
2. **Request a grant** for the CI role (and, if you preview as the viewer role,
   the viewer role) if access is legitimately needed. Don't hardcode credentials
   to work around it. Roles are the enforcement mechanism.
3. **For restricted (e.g. PII) schemas**, don't grant the app role access to the
   whole schema. Instead expose **only the columns you need** through a
   passthrough view in a governance source or the app-data schema (explicit column list, never `SELECT *`,
   so a future sensitive column can't leak), and point the app at that view.

## See also

- [Getting started](getting-started.md) — scaffold and preview an app.
- [Deploying](deploying.md) — ship it once the queries are wired.
- [`streamsnow.config.example.yaml`](../streamsnow.config.example.yaml) — the
  governance section in context.
