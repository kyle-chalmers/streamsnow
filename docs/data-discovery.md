# Data discovery

How to find tables and columns in Snowflake when you're building a dashboard,
and how to wire queries so they pass StreamSnow's governance checks. Table names
are **not** hardcoded anywhere — `INFORMATION_SCHEMA` is the source of truth.

Throughout, `<database>` is your `governance.database` and the schemas are your
`governance.schema_allow` from `streamsnow.config.yaml`.

## The two queries you need

**List the tables/views your apps are allowed to query:**

```sql
SELECT table_schema, table_name, table_type, comment
FROM <database>.INFORMATION_SCHEMA.TABLES
WHERE table_schema IN ('ANALYTICS', 'REPORTING')   -- your schema_allow
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

StreamSnow enforces schema access as **executable guardrails**, not convention:

- **`schema_allow`** — the only schemas your app code may reference.
- **`schema_deny`** — schemas that are blocked outright (raw/landing/bridge
  layers, ETL intermediates). `streamsnow check schema-refs` flags any reference
  to them in committed `apps/**` SQL or Python, and it runs in pre-commit, in
  `validate-app`, and in CI. Investigating a denied schema in Snowsight is fine;
  shipping a query against one is not.
- **`read_exceptions`** — specific fully-qualified objects (`DB.SCHEMA.OBJECT`)
  sanctioned for direct reads even if their schema isn't broadly allowed.

A reference that violates the policy fails the gate before it can merge.

## Rules of thumb

- **Prefer a curated/reporting layer over raw tables.** Reporting-style tables
  are narrow and pre-aggregated for dashboard query shapes; raw/analytics views
  are wide and heavy. Don't join three wide views for three columns.
- **Explicit column lists, never `SELECT *`.** Schema changes break `SELECT *`
  silently; explicit lists fail loudly. (This is a habit StreamSnow's checks
  don't enforce — adopt it anyway.)
- **Filter in SQL, not in Python.** Streamlit has a ~32 MB WebSocket message
  limit — push filters into `WHERE` so the returned DataFrame stays small.
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

1. **Check the role.** Your `secrets.toml` `role` must be your config's
   `viewer_role` — the role deployed apps run under. A query that works under a
   broad personal role but not `viewer_role` will ship as empty/erroring data.
2. **Request a grant** for `viewer_role` if access is legitimately needed — don't
   hardcode credentials to work around it. Roles are the enforcement mechanism.
3. **For restricted (e.g. PII) schemas**, don't grant the app role access to the
   whole schema. Instead expose **only the columns you need** through a
   passthrough view in an allowed schema (explicit column list, never `SELECT *`,
   so a future sensitive column can't leak), and point the app at that view.

## See also

- [Getting started](getting-started.md) — scaffold and preview an app.
- [Deploying](deploying.md) — ship it once the queries are wired.
- [`streamsnow.config.example.yaml`](../streamsnow.config.example.yaml) — the
  governance section in context.
