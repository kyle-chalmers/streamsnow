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

**files / layout.** Compare against a freshly scaffolded app rather than guessing, and check the
runtime first — container and warehouse expect different dependency manifests
([_shared/runtime-decision.md](../_shared/runtime-decision.md)).
