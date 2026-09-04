# Production lessons

Hard-won rules from running a fleet of Streamlit-in-Snowflake apps in
production. Each section names the failure mode first — if you're debugging,
scan the headings for your symptom. The enforceable subset is automated
(`streamsnow validate-app`, the deploy-safety hook, `streamsnow verify-deploy`,
`streamsnow check tombstones`, `streamsnow sql-review check`, the review-gate
Stop hook); the rest live here because they depend on warehouse state or
judgment that a static check can't see.

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

## Import shared page helpers package-qualified

**Symptom:** every page in a nav group dies with `ModuleNotFoundError: No
module named '_shared_header'` — after the change shipped with green CI, a
local boot against live Snowflake, and a click-through of all four pages that
reported zero console errors.

Deployed, **only the app root is on `sys.path`**. `streamlit run` *additionally*
puts the executing page's own directory there. So a helper at
`pages/_shared_header.py` imported by a sibling page as `from _shared_header
import ...` resolves locally and fails deployed — and the
local environment is structurally incapable of showing you the difference. This
is the one to disbelieve last: a full UI walkthrough passing is not evidence.

The same asymmetry has a quieter form. If a name exists *both* in a page's own
directory and at the app root, the import silently resolves to a **different
file** in each environment — no error, just the wrong code running in
production.

Rules:

- Import anything under a subdirectory package-qualified: `from
  pages._shared_header import ...`, `from pages.admin._hdr import ...`.
- Bare imports are correct only for modules that sit *beside* the entrypoint
  (`branding`, `sql_loader`, `config`) — the app root is on `sys.path` in both
  environments.
- Never name a file in `pages/` after an app-root module, a standard-library
  module, or a dependency.
- No `pages/__init__.py` is needed; PEP 420 makes `pages` an implicit namespace
  package once the app root is on `sys.path`.
- Never "fix" this with `sys.path.append(...)` in a page. It papers over the
  layout and hides the next instance.

The `page-imports` check blocks all of these statically, because nothing else
can.

## Retire apps by moving, not deleting

The deploy workflow scopes to `apps/**`, so retiring an app is a `git mv` to
`retired_apps/<slug>/` **plus a tombstone entry in the same PR**: the move
stops the app being deployed or scanned while the source stays in-tree as
documentation, and the entry in `deploy/tombstones.yml` is what the generated
CI demands — `streamsnow check tombstones` blocks a PR that stops declaring an
identifier without one — and what the deploy workflow's reconcile step then
drops (next section). Ad-hoc `DROP STREAMLIT <fqn>` from a session is gated by
the deploy-safety hook on purpose: destruction goes through the registry.

## A CREATE OR REPLACE pipeline has no delete path — tombstone what you abandon

**Symptom:** an app directory was renamed (or removed), and the *old* deployed
object is still live in Snowflake — frozen at the source of the last merge
that deployed it, flagged unhealthy by `streamsnow verify-deploy` on every
later merge, and cleaned up by nothing, because nothing left in the repo knows
it exists.

The pipeline only ever runs `CREATE OR REPLACE STREAMLIT`. The slug *is* the
object identity, so `git mv apps/a apps/b` doesn't rename the deployed object —
it mints a **new** object with a **new URL** and abandons the old one. In the
fleet this convention comes from, three rename PRs left four such orphans
before the rule was automated.

The organizing principle: **detection is automated and total; destruction
requires explicit committed consent.**

- *Detection:* `streamsnow check tombstones` (generated CI runs it on every
  PR) diffs declared identifiers against `origin/main` and blocks a PR that
  stops declaring one without a tombstone — the author of that PR is the one
  person who still has the context to say whether the object should die or the
  directory should be restored.
- *Consent:* `deploy/tombstones.yml` — identifier, reason, date, committed in
  the same PR as the rename/removal.
- *Execution:* the deploy workflow's reconcile step (`check tombstones
  --drop-sql`) drops every registered identifier on every deploy. `IF EXISTS`
  makes re-runs no-ops; a malformed registry exits 2 before any DROP; and the
  step refuses outright if a tombstone matches a currently-declared app —
  dropping that would kill the app the same deploy just created.

## Review coverage is per-change, not per-app (and never time-based)

**Symptom:** either the review nag never fires (an app was "reviewed once" so
everything after rides free), or it fires right after a review finishes
(a timestamp check sees the review's own fix commits as newer than the
review).

"Has this app been reviewed" is the wrong question — what matters is whether
*the changes being shipped* were reviewed. `streamsnow review-gate` therefore
stamps review artifacts with a per-file coverage key computed from the **AST
shape** (comments and docstrings stripped): reword a comment in a reviewed
file and it stays reviewed; change a line of logic and only that file reopens.
The same shape decides triviality, so a change too trivial to require review
is also too trivial to invalidate one. A file-mtime rule can't do this — the
review loop writes its report *and then* commits fixes, so mtime marks the
review it just finished as stale and nags after every successful run.

Delivery matters as much as the decision. The nudge is a warn-only `Stop`
hook, and its payload is `systemMessage`-only, **measured, not assumed**
(2026-08-04, in the source fleet): emitting `additionalContext` from a Stop
hook does not queue a reminder — it starts a fresh assistant turn with no user
input, costing an unrequested turn per change. Don't switch it to a richer
payload without re-measuring.

## Rendered SQL nobody can re-run rots — hash the audit trail

**Symptom:** the "reviewed SQL" a dashboard's numbers were signed off on no
longer matches what the app executes — a template or filter changed after the
review file was written, and the file kept looking authoritative.

Apps store `{TOKEN}` + `:N` templates a reviewer can't paste into Snowsight,
so hand-rendered review copies get written once, drift silently, and end up
*worse* than nothing: they document a query the app no longer runs. The fix is
to make the rendered copy a build product, not prose:

- Each feature's filter combos, token values, and bind placeholders live in a
  JSON **manifest** under `apps/<slug>/sql_review/manifests/` (in the app dir
  on purpose — renaming or retiring the app moves its audit trail with it).
- `streamsnow sql-review generate` renders the paste-runnable `.review.sql`
  files and stamps each with a **provenance line** — content hashes of every
  input (manifest, query templates, dispatcher modules) and of the rendered
  output itself.
- `streamsnow sql-review check` recomputes both hashes **without importing any
  app code** — a shared pre-commit/CI hook that imports consumer modules would
  execute arbitrary code on every commit — so an edited template, manifest, or
  hand-edited rendered file all read as DRIFT, and every `queries/*.sql` must
  be claimed by some manifest (a query the generator can't account for is a
  named failure, never a silent skip).

Rendered files are also verified against a statement-root allowlist (`SELECT`
/ `WITH…SELECT` / `SHOW` / `DESCRIBE` / `EXPLAIN` / session-variable `SET`) —
an allowlist rather than a write-verb denylist *as the primary guard*, because
the failure mode of a denylist is the statement type nobody thought of.

Underneath it sits a second, independent layer, added in 0.6.2 after the
allowlist was defeated four separate times. Every one of those bypasses worked
the same way: hide a `)` inside a quoting form the masker did not know about
(a single-quoted string, a double-quoted identifier, a dollar-quoted constant,
a backslash-escaped quote), so the CTE scan ended early and a trailing
`SELECT` read as the statement's terminal verb while the engine would run the
`DELETE`. Each individual fix was correct and the next quoting form still got
through, which is the real lesson: an allowlist is only as good as its ability
to find statement boundaries, and that is a parsing problem that keeps having
one more case.

So a write verb in command position is refused independently of the walker.
Two anchors: at the START of a statement any of them is a command, and right
after a `)` the reserved words are, plus the non-reserved ones in two-token
command form (`MERGE INTO`, `TRUNCATE [TABLE]`, `COMMENT ON <object-type>`, …).
There is no `;` anchor — statements are split on `;` before this runs. It does
consult small keyword lists rather than being parse-free, because a bare column
or JOIN alias may legally be named after a non-reserved verb; that is the cost
of not refusing legitimate SQL. And a masking form left unterminated makes the
rest of the file unreadable, so it is refused outright rather than passed
unchecked. Note this is not the denylist the paragraph above warns against: as a
*sole* guard a denylist fails on the verb nobody listed, but as a second layer
under an allowlist it costs nothing and catches the parser failure. Position
matters because most of these verbs are not Snowflake reserved words —
`SELECT 1 AS CALL` is legal read-only SQL, and a blanket token match refused
it. Refusing to generate a legitimate audit file is its own defect. Scope honesty: the hashes
catch accidents and drift, not a deliberate committer — repository review
remains the trust boundary for malicious commits.
