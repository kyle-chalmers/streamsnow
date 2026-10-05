# Fixing each failing check

In the order the gate prints them. Each check's name is exactly what `streamsnow validate-app` shows.

**required-files.** Check the runtime first, the way the checker does: from the app's own
`snowflake.yml` (anchored `runtime_name:` key; config's top-level `runtime` is only the fallback).
Every app needs `streamlit_app.py`, `snowflake.yml`, `branding.py`, `sql_loader.py`, `AGENTS.md` and
`.streamlit/config.toml`; container adds `pyproject.toml`, warehouse adds `environment.yml`. A common
false alarm is judging a container app against warehouse expectations
([_shared/runtime-decision.md](../_shared/runtime-decision.md)). Compare against a freshly
scaffolded app rather than guessing.

**manifest.** The finding names the field. `snowflake.yml`: `definition_version: 2`, `main_file:
streamlit_app.py`, an `identifier` mapping, and a `query_warehouse` from config's allowed list.
Container: `runtime_name` equal to config's, a `compute_pool`, and a non-empty
`external_access_integrations`; warehouse must not declare those container-only fields.
`pyproject.toml` (container) needs a `[project]` name, a `requires-python` that allows the container
Python, and `streamlit` plus `snowflake-snowpark-python` in `dependencies`. `environment.yml`
(warehouse) needs a `name`, the same two packages, and **no `python` pin**: the runtime supplies
Python, and a pin breaks `CREATE STREAMLIT`.

**artifacts.** `snowflake.yml`'s `artifacts:` list disagrees with the files on disk. Local dev reads
disk while a manifest-driven deploy reads the list, so an uncovered file works locally and silently
goes missing deployed; a stale entry breaks the deploy. `streamsnow check artifacts --fix
apps/<slug>` repairs the block from disk; otherwise add the new file (or its parent `dir/` entry),
or remove entries for deleted files. Never delete a file that a page still imports. Removing the
whole `artifacts:` key is valid only if your deploy provably uploads the entire app dir
(StreamSnow's generated workflows do).

**artifacts, when a file is shipped by another step.** If the repo's deploy pipeline uploads a
non-code file separately (the generated workflow does this for `.streamlit/config.toml`), list it
under `deploy.artifact_exclude` in `streamsnow.config.yaml` rather than in `artifacts:`. Only
non-code files qualify; the entrypoint, `pages/`, `queries/`, `*.py` and `*.sql` are always
artifacts and the config loader rejects them.

**naming.** The app folder name must match `^[a-z][a-z0-9-]*$`: lowercase, starting with a letter,
hyphens not underscores. Renaming an app is a human decision (its deployed identifier and URL
follow), so hand it back rather than moving the folder.

**schema-refs.** Code references a schema in `governance.schema_deny`. Fix by routing the query
through an allowed schema, typically a curated reporting/analytics view, never by editing the deny
list: changing governance to pass the check defeats the check. A single object that must stay
readable belongs in `governance.read_exceptions` (exact fully-qualified name), and that is a human
governance decision, not a mechanical fix.

**app-security.** Each finding names its kind; all are mechanical to locate, some judgment-bound to fix:

- *egress*: networking/exfil imports. Remove; an in-Snowflake app shouldn't reach the network.
- *code-exec*: `eval` / `exec` / `os.system` / `subprocess` / `pickle` and friends. Remove or replace.
- *write-sql*: a statement starting with `DROP`, `DELETE`, `TRUNCATE`, `INSERT`, `UPDATE`, `MERGE`,
  `UPSERT`, `REPLACE`, `CREATE`, `ALTER`, `GRANT`, `REVOKE` or `CALL` in a `.sql` file or a constant passed
  to `.sql()` / `.query()`. Apps are read-only; the write doesn't belong in app code. A DDL file
  directly in an app's `sql_review/app_specific_reporting_objects/` may use `CREATE`, `ALTER` and
  `GRANT` (that folder holds the maintained object definitions); any other write there still fails.
- *dynamic-sql*: SQL assembled by f-string / `.format` / `%` / `+` at a `.sql()` / `.query()` call.
  Fix with bind parameters, or a `{TOKEN}` fragment validated against an allowlist. Never paper
  over it by string-escaping.
- *snowflake-cortex-rest*: a `requests` import waived for Cortex Analyst whose code departs from
  the one allowed shape (the Cortex Analyst endpoint, a URL built from `SNOWFLAKE_HOST`, the
  session token file, one `requests.post`). Match that shape exactly or remove the waiver.
- *syntax*: the file doesn't parse, so nothing else in it was checked. Fix the syntax error first.

**bind-predicates.** The `:N IS NULL OR col = :N` pattern (an "All" sentinel binding `None`) works
locally but breaks deployed: the deployed Go driver NULL-binds the *whole* parameter list when any
one value is `None`. Classic symptom: KPIs fine in preview, 0/0 deployed. Fix by building the
predicate fragment only when a real value is supplied (a `{TOKEN}` fragment rendered in), so `None`
never reaches a bound position.

**sql-tokens.** A `{TOKEN}` placeholder appears inside a SQL comment. `render_sql` substitutes
tokens with comment-unaware `str.replace`, so the token's full SQL expansion lands inside the
comment and multi-line expansions break out as live SQL (parse errors that only appear at render
time). Fix by describing the token in prose (`-- agent filter applied here`), never by braces;
`-- noqa: sql-token` only for a comment that genuinely must show the syntax.

**session-fallback.** A `get_active_session()` call isn't wrapped in a broad `try/except`. The call
raises during local `streamlit run` (it only works inside the deployed runtime), and a narrow
`except ImportError` misses resolver-dependent failure types. Fix with the scaffold shape: call
inside `try:`, `except Exception:` falling back to `st.connection("snowflake").session()`.

**page-imports.** A bare import of a module that lives in a subdirectory (`from _layout import ...`
for the scaffold's `pages/_layout.py`). Deployed, only the app root is on `sys.path`; `streamlit
run` also adds the executing page's own directory, so this class boots clean locally, survives a
full UI walkthrough, and then raises `ModuleNotFoundError` on every affected page in production.
Believe the check, not the walkthrough. Fix by qualifying the import against the app root (`from
pages._layout import ...`, `from pages._data import ...`, `from pages.admin._hdr import ...`), never
with a `sys.path.append` shim in the page, which hides the next instance. The *ambiguous* variant
(a name that exists both in the page's own directory and at the app root, in stdlib, or in a
dependency) resolves to a different file in each environment; fix it by qualifying or renaming,
since one of the two files is being silently ignored somewhere. No `pages/__init__.py` is required:
PEP 420 namespace packages cover it.

**caching.** Every public function that runs a named query (a string literal, `load_sql(...)` or
`render_sql(...)`) needs `@st.cache_data` with an explicit `ttl=` keyword. A loader more than one
page uses belongs in `pages/_data.py`, decorated there once, so pages never cache the same query
twice. The check follows delegation to private helpers only within one file, so a passing check
is not proof that a cross-module loader is cached: read it. An intentional uncached call (e.g. a
connection heartbeat where a stale cached result would hide a dead session) takes
`# noqa: cache-required` and a line in the app's `AGENTS.md`, not a reason to drop caching
broadly. An app with no data fetches legitimately has nothing to cache.

**path-leaks.** A `.py` or `.md` file carries a personal absolute path: `C:\Users\<user>\`,
`/Users/<user>/Development/`, or `/home/<user>/` (the CI `runner` home is allowed). Replace it with
a path relative to the repo, a docs link, or a `<user>` placeholder, or delete the leftover note.
There is no waiver.

**requirements.** The §11 Build Progress block of `REQUIREMENTS.md` (any under the app) is what
`/build-app` resumes from. It needs a `## 11. ... Build Progress` heading; a `**Current phase:**`
line whose value is exactly one of `spec`, `discover`, `design`, `scaffold`, `build`, `preview`,
`verify`, `ship`, `done`, `in-production` or `in-production (backfilled)`; and a `### Sessions` log
whose last bullet starts with an ISO date and, unless the phase is `done`, `in-production` or
`in-production (backfilled)`,
contains `Next:`. Progress narrative ("pages 3/5", "QC pending") goes on a `**Phase notes:**` line
under the phase, never appended to it. If the check rejects `discover` or `design`, the installed
`streamsnow` CLI is older than the plugin: upgrade it rather than changing the phase.

**sql-review.** Each finding names its `kind`; fix the source, then
`streamsnow sql-review generate <slug>`, never the generated page file:

- `index`: fix `sql_review/index.yaml` as the message says. A page listed there but missing from the
  navigation is one; keep `pages/about.py` (`metrics: []`) listed while About is in the nav.
- `provenance`: regenerate; a hand edit or a stale README table reads here too.
- `marker`: make the page's `review_value("<key>", …)` calls and the page's metrics in
  `index.yaml` match, one call per metric.
- `objects`: the DDL file header, its `objects:` entry, and the metrics' `reads:` must agree.
- `lint`: a sqlfluff finding in `queries/*.sql` (rule code first; `sqlfluff fix` handles most
  layout ones, and the repo's `.sqlfluff` tunes the rules), or a page-file section that doesn't parse.
- `comments`: a CTE without a one-line comment directly above its name, or a comment line over
  100 characters.
- `readonly` / `bind`: a section that would write or does not run.
- `coverage` (fails only under `sql_review.coverage: fail`): a nav page or a `queries/*.sql` file
  that `index.yaml` doesn't account for. List the page (with `metrics: []` if it shows no data). A
  query, including a shared one behind a `pages/_data.py` loader, is covered when it is the `query`
  of a metric on any page that shows its result (with that visual's `review_value` marker), or,
  when it is only inlined into other queries, listed under `fragments:` with a `reason`. A query
  that fits neither (date bounds, a filter's value list) is a judgment call: hand it back with the
  warning rather than inventing a metric.
- `advisory`: never fails (comment density over 25%).

Writing new `index.yaml` entries means choosing real sample values; that is a judgment, not a
mechanical fix. Hand it to `/sql-review <slug> --offline`, which builds the index per
[sql-review/authoring.md](../sql-review/authoring.md) without touching the warehouse (inside
`/build-app`, its build step owns `index.yaml`).

**placeholders.** An app `.py`, `.sql`, `.json` or `.yaml` file still contains the scaffold's
`YOUR_TABLE`, or `pages/overview.py` still shows the starter's hard-coded sample metric and chart
(the block marked `STREAMSNOW_STARTER_PLACEHOLDER`): the starter trio from `streamsnow new`
(`queries/example_metric.sql`, its `index.yaml` entry and review window, and `pages/overview.py`)
was never replaced. The sample values never read the query, so repointing `YOUR_TABLE` alone does
not clear the page. Replace the trio the way
[build-app's build phase](../build-app/pages.md#replace-the-starter-trio) does, or repoint the query
and the `index.yaml` review window at a real table and render the query's results in place of the
sample block (keep its `review_value("example_metric", …)` wrapper). Either way, keep the
`pages/about.py` entry in `index.yaml`. Never rename the token or delete only the marker to dodge
the check: CI deploys every app under `apps/`, and this is what stops a placeholder app shipping.

**Waivers, when a rule is knowingly not applied.** Each sits on the offending line:
`# noqa: dynamic-sql` (the only `app-security` noqa; none waives egress, code-exec or write-sql),
`# snowflake-cortex-rest` on a Cortex Analyst `import requests` (the one egress exception, and only
in its exact shape), `# noqa: cache-required`,
`# noqa: session-fallback` on the `get_active_session()` call, `# noqa: page-imports` on the import,
and `-- noqa: sql-token` on the SQL line. A waiver is a decision the diff should explain in a nearby
comment, never a way to make a red gate green.
