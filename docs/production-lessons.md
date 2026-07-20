# Production lessons

Hard-won rules from running a fleet of Streamlit-in-Snowflake apps in
production. Each section names the failure mode first — if you're debugging,
scan the headings for your symptom. The enforceable subset is automated
(`streamsnow validate-app`, the deploy-safety hook, `streamsnow verify-deploy`);
the rest live here because they depend on warehouse state or judgment that a
static check can't see.

The condensed symptom→rule version for agent sessions is
[`skills/_shared/production-gotchas.md`](../skills/_shared/production-gotchas.md).

## Deployed apps read with the owner's grants, not the viewer's

**Symptom:** a query works locally and in preview, then fails with
`does not exist or not authorized` only in the deployed app.

A deployed Streamlit app executes with **owner's rights** — the grants of the
role that owns/deploys the object (your `snowflake.roles.ci_role`), not the
viewer's role and not your personal role. Local previews run with *your*
credentials, so anything your (usually broader) role can read compiles and
renders locally while the deployed app 403s.

Rules:

- Before shipping a page that touches a new table/view, verify the **ci_role**
  can `SELECT` it — not your role, not the viewer role.
- Prefer standing **future grants** so new objects are zero-touch:
  `GRANT SELECT ON FUTURE TABLES IN SCHEMA <db>.<schema> TO ROLE <ci_role>;`
  (and the same for `VIEWS`, and `DYNAMIC TABLES` if you use them).
- When a source moves schemas, re-verify: the future grant lives on the schema,
  not the object.

## Expose restricted data through narrow passthrough views

**Symptom:** an app needs one fact that lives in a denied/restricted schema
(raw ingestion, PII), and the temptation is to grant the app role into it.

Don't widen grants — build a **narrow, projection-only view** in an allowed
schema, owned by a role that can read the restricted base. The base resolves
with the *view owner's* rights; the app reads only the view and stays inside
its allowed schemas. Drop every column the app doesn't need (especially direct
identifiers) at the view boundary.

Performance corollary: keep the passthrough **non-secure and join-free**.
Snowflake will not push an outer `WHERE` predicate through a secure view or
across an embedded large join — one production view that wrapped a big join
forced a full base scan on every query. The fix was a projection-only view
(so the planner flattens it and predicates prune the base scan) with the join
moved to the consuming query, filtering the small side first.

## Dynamic tables have their own role and layering rules

Three platform constraints that only surface at refresh time:

- **A dynamic table's scheduled refresh runs as its owner's PRIMARY role
  only** — secondary-role grants don't apply during refresh. If the DT's
  sources are readable only via a secondary role, the refresh fails even
  though `CREATE DYNAMIC TABLE` succeeded interactively. Source the DT from a
  view the primary role can read instead of from the restricted base directly.
- **A dynamic table cannot read a view that references other dynamic
  tables.** Keep DT sources "DT-safe" (views over base tables, config tables);
  joins to DT-backed views belong in the downstream view layer, not in the DT.
- **Manual `ALTER DYNAMIC TABLE ... REFRESH` needs OPERATE on every upstream
  DT** — which your role may lack for externally-owned sources. Wait for the
  scheduled `TARGET_LAG` refresh, or (for `DOWNSTREAM` lag) query a downstream
  consumer to trigger it.

## Keep an audit trail for out-of-band DDL

Apps often consume objects (passthrough views, grants, dynamic tables) that
live outside the app repo's deploy scope. Record that DDL as dated SQL files in
a `migrations/`-style directory: `YYYY-MM-DD-<description>-<object>.sql`,
run manually by whoever holds the privilege, committed in the same PR as the
consuming app change.

- CI does **not** run these files — the live object is the source of truth
  (recoverable via `GET_DDL`), the files are the audit trail of what was run
  and why.
- Header-comment each file with the grants applied (or "grants: none — future
  grants cover it") and how you verified the ci_role can read the result.

## Local dev caches SQL text at the process level

**Symptom:** you add a column to a query file, the page hot-reloads, and the
new column isn't there.

Streamlit's hot-reload picks up `.py` edits, but query *results* cached in the
process can still reflect the old SQL text. A page refresh is not enough —
fully restart `streamlit run` before concluding a SQL change "didn't work".
(This is a local-only cousin of the local-vs-deployed divergences above.)

## Verify the deployed runtime before diagnosing feature compatibility

**Symptom:** a plausible-sounding fix ("this Streamlit feature doesn't exist in
Snowflake, remove it") for an app that may not even be broken.

`snowflake.yml` declares the *intended* runtime; the authoritative answer is
live: `SHOW STREAMLITS` / `DESC STREAMLIT <fqn>` on the deployed object.
Container and warehouse runtimes run very different Streamlit versions with
different feature support — a feature-compatibility diagnosis is only valid
relative to the runtime the app actually runs on. Check first; a wrong
"harmless" fix removes working functionality and entrenches the misdiagnosis.

Related: pin the container app's `streamlit` version to **at least the base
image's bundled version**. The base image's launcher passes flags from its own
Streamlit build; an older pin rejects unknown flags and crash-loops on startup
while the service still reports healthy (`streamsnow verify-deploy` scans the
service logs for exactly this).

## Never pass Python `None` in `params=` to a deployed query

The deployed connection middleware NULL-binds **every** positional parameter
when **any** one is `None` — predicates silently become `BETWEEN NULL AND
NULL`, the query "succeeds" with zero rows scanned, and the local Python
connector does *not* reproduce it. Build optional filters with `render_sql`
token substitution instead of nullable params. The decidable shape of this bug
(`(:1 IS NULL OR col = :1)`) is blocked by the `bind-predicates` check; the
general rule — no `None` in `params=`, ever — is yours to hold.

## Retire apps by moving, not deleting

The deploy workflow scopes to `apps/**`, so retiring an app is a `git mv` to
`retired_apps/<slug>/`: it stops deploying and stops being scanned with no
pipeline change, and the source stays in-tree as documentation. The pipeline
never drops Snowflake objects — `DROP STREAMLIT <fqn>` is a deliberate manual
step (the deploy-safety hook gates it on purpose).
