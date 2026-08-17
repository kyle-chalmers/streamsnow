# Fixing each failing check

**schema-refs.** Code touches a schema in `governance.schema_deny` (or never touches an allowed
one). Fix by routing the query through an allowed schema — typically a curated reporting/analytics
view — never by editing the deny list; changing governance to pass the check defeats the check. A
genuinely required denied reference is a human governance decision, not a mechanical fix.

**security.** Four classes, all mechanical to locate, some judgment-bound to fix:

- *egress* — networking/exfil imports. Remove; an in-Snowflake app shouldn't reach the network.
- *exec* — `eval` / `exec` / `os.system` / `subprocess` / `pickle` and friends. Remove or replace.
- *write-sql* — `DROP`/`DELETE`/`INSERT`/`UPDATE`/`MERGE`/`CREATE`/`ALTER`/`GRANT` in SQL or inline
  constants. Apps are read-only; the write doesn't belong in app code.
- *dynamic-sql* — SQL assembled by f-string / `.format` / `%` / `+`. Fix with bind parameters, or a
  `{TOKEN}` fragment validated against an allowlist. Never paper over it by string-escaping.

**bind-predicates.** The `:N IS NULL OR col = :N` pattern (an "All" sentinel binding `None`) works
locally but breaks deployed: the warehouse driver NULL-binds the *whole* parameter list when any one
value is `None`. Classic symptom — KPIs fine in preview, 0/0 deployed. Fix by building the predicate
fragment only when a real value is supplied (a `{TOKEN}` fragment rendered in), so `None` never
reaches a bound position.

**caching.** Every data-fetching function needs `@st.cache_data(ttl=<positive int>)`. An intentional
uncached call (e.g. a connection heartbeat where a stale cached result would hide a dead session) is
an exception to document in the app's `AGENTS.md`, not a reason to drop caching broadly. An app with
no data fetches legitimately has nothing to cache.

**artifacts.** `snowflake.yml`'s `artifacts:` list disagrees with the files on disk. Local dev reads
disk while a manifest-driven deploy reads the list, so an uncovered file works locally and silently
goes missing deployed; a stale entry breaks the deploy. Fix by adding the new file (or its parent
`dir/` entry) to the list, or removing entries for deleted files — never by deleting a file that a
page still imports. Removing the whole `artifacts:` key is valid only if your deploy provably uploads
the entire app dir (StreamSnow's generated workflows do).

**sql-tokens.** A `{TOKEN}` placeholder appears inside a SQL comment. `render_sql` substitutes
tokens with comment-unaware `str.replace`, so the token's full SQL expansion lands inside the
comment and multi-line expansions break out as live SQL (parse errors that only appear at render
time). Fix by describing the token in prose (`-- agent filter applied here`), never by braces;
`-- noqa: sql-token` only for a comment that genuinely must show the syntax.

**session-fallback.** A `get_active_session()` call isn't wrapped in a broad `try/except`. The call
raises during local `streamlit run` (it only works inside the deployed runtime), and a narrow
`except ImportError` misses resolver-dependent failure types. Fix with the scaffold shape: call
inside `try:`, `except Exception:` falling back to `st.connection("snowflake").session()`.

**page-imports.** A bare import of a module that lives in a subdirectory (`from _header import ...`
for `pages/_header.py`). Deployed, only the app root is on `sys.path`; `streamlit run` also adds the
executing page's own directory, so this class boots clean locally, survives a full UI walkthrough,
and then `ModuleNotFoundError`s on every affected page in production — believe the check, not the
walkthrough. Fix by qualifying the import against the app root (`from pages._header import ...`,
`from pages.admin._hdr import ...`) — never with a `sys.path.append` shim in the page, which hides
the next instance. The *ambiguous* variant (a name that exists both in the page's own directory and
at the app root, in stdlib, or in a dependency) resolves to a different file in each environment;
fix it by qualifying or renaming, since one of the two files is being silently ignored somewhere.
No `pages/__init__.py` is required — PEP 420 namespace packages cover it.

**files / layout.** Compare against a freshly scaffolded app rather than guessing, and check the
runtime first — container and warehouse expect different dependency manifests
([_shared/runtime-decision.md](../_shared/runtime-decision.md)).
