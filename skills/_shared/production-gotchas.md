# production-gotchas

Purpose: symptom → rule lookup for production traps that static checks can't
catch. Skills consult this when a behavior diverges between local and deployed,
or when touching warehouse objects an app depends on. Full rationale and SQL
recipes: [`docs/production-lessons.md`](../../docs/production-lessons.md).

| Symptom | Rule |
|---|---|
| Works locally / in preview, `not authorized` deployed | Deployed apps run with the **ci_role's** (owner's) grants, not yours and not the viewer's. Verify the ci_role can SELECT every source; prefer `GRANT SELECT ON FUTURE ...` per schema so new objects are zero-touch. |
| App needs one fact from a denied/restricted schema | Don't widen grants — add a narrow **projection-only passthrough view** in an allowed schema, owned by a privileged role. Keep it non-secure and join-free so predicates push down to the base scan. |
| Dynamic table refresh fails though CREATE worked | DT refresh runs as the **owner's primary role only** (secondary roles ignored). Source the DT from a view the primary role reads, not the restricted base. |
| `CREATE DYNAMIC TABLE` rejects a source view | A DT can't read a view that references other DTs. Keep DT sources DT-safe; put DT-joins in the downstream view layer. |
| Manual `ALTER DYNAMIC TABLE ... REFRESH` not authorized | Needs OPERATE on all upstream DTs. Wait for `TARGET_LAG`, or query a downstream consumer to trigger `DOWNSTREAM` lag. |
| SQL edit "has no effect" during local dev | Hot-reload picks up `.py` edits, not cached query results of changed SQL text. Fully restart `streamlit run` before concluding anything. |
| "Feature X crashes in Snowflake" diagnosis | Verify the **actual** runtime first: `SHOW STREAMLITS` / `DESC STREAMLIT <fqn>` — `snowflake.yml` only declares intent. Container vs warehouse run very different Streamlit versions. |
| Container app crash-loops after a platform update | The base image passes launcher flags from its own Streamlit build; pin the app's `streamlit` to at least the base image's version. `streamsnow verify-deploy <slug>` detects the `No such option` log signature. |
| Query "succeeds" deployed but returns nothing | Never pass Python `None` in `params=` — the deployed driver NULL-binds ALL positional params when any is None (local doesn't reproduce). Use `render_sql` tokens for optional filters. |
| Every page in a group renders locally, `ModuleNotFoundError` deployed | Deployed, **only the app root is on `sys.path`**; `streamlit run` also adds the executing page's own directory. Import subdirectory helpers package-qualified (`from pages._header import ...`). A local boot and a full UI walkthrough cannot catch this — `streamsnow check page-imports` can. |
| App consumed DDL that lives outside this repo | Record it as a dated SQL file in a `migrations/`-style dir, committed with the consuming PR; live object is the source of truth, the file is the audit trail. |
| Retiring an app | `git mv apps/<slug> retired_apps/<slug>` (deploy scopes to `apps/**`). Dropping the Snowflake object is a separate deliberate manual step. |

Contract: this file is read-only guidance — nothing here authorizes running
DDL, widening grants, or bypassing the schema deny-list. Grant and view
creation are account-owner actions; skills propose the SQL and stop.
