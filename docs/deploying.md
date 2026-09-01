# Deploying

StreamSnow scaffolds a `.github/workflows/deploy.yml` that ships your apps to
Snowflake on merge to `main`. It **skips automatically while the
`SNOWFLAKE_ACCOUNT` secret is unset** — that one secret is the gate, so it
never fails a normal merge before you're ready. Once `SNOWFLAKE_ACCOUNT` is
set, the job runs, and will fail at authentication if the other secrets are
still missing.

This is the end-to-end picture. For the one-time Snowflake objects and the exact
CI secret list, see **[Deploy setup](deploy-setup.md)**.

## How a deploy runs

On merge to `main`, the workflow:

1. Authenticates to Snowflake as your `ci_role` (key-pair / JWT).
2. Makes the app source available to Snowflake — how depends on your
   **[deploy source](#two-deploy-sources)**.
3. Runs `CREATE OR REPLACE STREAMLIT` for each app under `apps/` via
   `streamsnow deploy-sql`.
4. **Reconciles tombstones** — drops every retired identifier listed in
   `deploy/tombstones.yml` (see
   [Retiring or renaming an app](#retiring-or-renaming-an-app)).
5. **Verifies deploy health** per app (`streamsnow verify-deploy`) — object
   exists, live version set, no container crash-loop signature. With the
   **stage-copy** source it also confirms the version source matches the merge
   SHA. The **git-repository** workflow verifies health only — it calls
   `verify-deploy` without `--sha` (the fetch/refresh step is what advances
   versions there), and that refresh step is best-effort: a failed refresh
   logs and continues rather than failing the run.

The scaffolded checks workflow runs `validate-app` on every PR, but nothing
wires it to the deploy job: `checks.yml` and `deploy.yml` are independent
workflows, and StreamSnow does not scaffold branch protection. The gate holds
only if your repo requires the checks before merge — recommended: add a GitHub
branch protection rule (or ruleset) on `main` that lists the checks jobs as
required status checks. With that in place, the deploy job can safely assume
merged code already passed.

An app's `sql_review/` directory (the paste-runnable SQL audit trail that
`streamsnow sql-review` maintains) is a **repo-side artifact for human
reviewers** — the running app never reads it, and the scaffolded
`snowflake.yml` does not declare it among the app's `artifacts:`. It exists so
someone can re-run each visual's SQL in Snowsight, not to ship.

## Retiring or renaming an app

The pipeline above only ever runs `CREATE OR REPLACE` — it has **no implicit
delete path**. Renaming `apps/<a>/` to `apps/<b>/` mints a *new* object with a
*new* URL; removing a directory just stops re-deploying the old object. Either
way, the previously deployed STREAMLIT lives on, frozen at the last merge that
deployed it, and `verify-deploy` flags it on every later run.

The delete path is explicit and consent-based:

1. In the **same PR** that renames or removes the app directory, add the
   abandoned identifier to `deploy/tombstones.yml` (identifier, reason, date).
   The generated CI runs `streamsnow check tombstones` and **blocks** a PR
   that abandons an identifier without a tombstone.
2. On the next merge, the deploy workflow's **Reconcile tombstones** step runs
   `streamsnow check tombstones --drop-sql` and executes the emitted
   `DROP STREAMLIT IF EXISTS <identifier>;` statements. `IF EXISTS` makes the
   step idempotent — every deploy re-drops the registry and re-runs are
   no-ops.
3. Two refusals guard the DROP: a malformed registry exits before any
   statement is emitted, and a tombstone that matches a **currently declared
   app** makes the step fail outright — dropping it would kill the app this
   very deploy just created (the reconcile step re-checks this itself because
   a direct push to `main` never went through the PR check).

## Two deploy sources

Set `deploy.source` in `streamsnow.config.yaml`:

| | **stage-copy** (default) | **git-repository** |
|---|---|---|
| Mechanism | CI uploads `apps/` to a SHA-versioned internal stage; the STREAMLIT serves `FROM '@stage/...'` | Snowflake's `GIT REPOSITORY` object fetches the app source from your Git repo |
| Network direction | CI → Snowflake only | Snowflake → GitHub (must be reachable) |
| One-time objects | an internal stage | API integration + secret (GitHub token) + `GIT REPOSITORY` |
| Best when | you want the fewest moving parts and no Snowflake→GitHub dependency | you already run a Snowflake `GIT REPOSITORY` workflow |

The scaffold renders `deploy.yml` for whichever source your config declares.
With the default **stage-copy**, Snowflake never reaches out to GitHub, so
there's no network-policy dependency. Choose `git-repository` only if you
specifically want Snowflake to pull from your repo; the rendered workflow then
runs `snow git fetch` and Snowflake must reach GitHub (or you mint a
GitHub-App token into the secret).

## One-time setup

Generate and review the DDL for your configured source, then run it once with an
admin (or CI) role:

```bash
streamsnow deploy-setup | less                # review first
streamsnow deploy-setup | snow sql --stdin    # then apply
```

- **stage-copy**: creates the internal stage CI uploads to. **Container** apps
  also need an account-level `compute_pool` + `external_access_integration`
  (emitted as commented admin guidance — these reach PyPI for dependencies).
  **Warehouse** apps need neither.
- **git-repository**: creates the API integration, the secret holding a GitHub
  token, and the `GIT REPOSITORY` object, and grants them to your `ci_role`.

Then add the CI auth secrets (key-pair / JWT for the CI user). The full secret
table is in **[Deploy setup → CI auth](deploy-setup.md#2-ci-auth-key-pair--jwt)**.
Once `SNOWFLAKE_ACCOUNT` is present, the deploy job runs on the next merge.

## The per-app create statement

`streamsnow deploy-sql` emits the SQL the workflow runs — useful to inspect or
to deploy a single app by hand:

```bash
streamsnow deploy-sql <slug>                 # CREATE OR REPLACE STREAMLIT (stage-copy embeds the SHA)
streamsnow deploy-sql <slug> --sha <sha>     # pin a specific commit (stage-copy)
streamsnow deploy-sql <slug> --refresh       # git-repository: ABORT/PULL/COMMIT an existing app
```

`streamsnow stage-path` prints the stage base path (`@DB.SCHEMA.STAGE`) the
stage-copy upload targets.

## Runtime notes

- **Container** (`runtime: container`, default): the app's `snowflake.yml`
  declares `runtime_name`, `compute_pool`, and `external_access_integrations`;
  dependencies come from `pyproject.toml`. The compute pool + EAI must exist
  before the first deploy (see one-time setup).
- **Warehouse** (`runtime: warehouse`): no compute pool or EAI; dependencies come
  from `environment.yml` (Snowflake Anaconda channel). Never pin `python` there —
  the channel has no exact `python==3.11` build and it breaks `CREATE STREAMLIT`
  (`validate-app` flags this).

## Verifying a deploy

After the workflow runs, confirm the app exists and points at the expected
version:

```bash
snow sql -q "SHOW STREAMLITS IN SCHEMA <app_database>.<app_schema>;"
```

Open it in Snowsight under **Projects → Streamlit**. If a container app fails to
start, the usual causes are a missing compute pool / EAI, or the `query_warehouse`
not being granted to `viewer_role` (the manifest check flags an unlisted
warehouse before deploy).

## Re-rendering the pipeline after a config change

If you change deploy-related config (source, warehouse, roles), re-render the
generated governance files:

```bash
streamsnow update            # dry-run: shows what would change
streamsnow update --apply    # write the changes
```

`update` re-renders `AGENTS.md`, hooks, CI, and `deploy.yml` from your current
config; it leaves `README` and `.gitignore` alone.

## See also

- [Deploy setup](deploy-setup.md) — one-time Snowflake objects + CI secret list.
- [Getting started](getting-started.md) — scaffold, preview, validate.
- [Data discovery](data-discovery.md) — wire governed queries before you ship.
