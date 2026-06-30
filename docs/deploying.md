# Deploying

StreamSnow scaffolds a `.github/workflows/deploy.yml` that ships your apps to
Snowflake on merge to `main`. It **skips automatically** until you add the CI
secrets, so it never fails a normal merge before you're ready.

This is the end-to-end picture. For the one-time Snowflake objects and the exact
CI secret list, see **[Deploy setup](deploy-setup.md)**.

## How a deploy runs

On merge to `main`, for each app under `apps/`, the workflow:

1. Authenticates to Snowflake as your `ci_role` (key-pair / JWT).
2. Makes the app source available to Snowflake — how depends on your
   **[deploy source](#two-deploy-sources)**.
3. Runs `CREATE OR REPLACE STREAMLIT` for the app via `streamsnow deploy-sql`.

`validate-app` is the hard gate *before* a PR merges; the deploy job assumes the
merged code already passed it.

## Two deploy sources

Set `deploy.source` in `streamsnow.config.yaml`:

| | **stage-copy** (default) | **git-repository** |
|---|---|---|
| Mechanism | CI uploads `apps/` to a SHA-versioned internal stage; the STREAMLIT serves `FROM '@stage/...'` | Snowflake's `GIT REPOSITORY` object fetches the app source from your Git repo |
| Network direction | CI → Snowflake only | Snowflake → GitHub (must be reachable) |
| One-time objects | an internal stage | API integration + secret (GitHub token) + `GIT REPOSITORY` |
| Best when | you want the fewest moving parts and no Snowflake→GitHub dependency | you already run a Snowflake `GIT REPOSITORY` workflow |

The scaffolded `deploy.yml` targets **stage-copy** — Snowflake never reaches out
to GitHub, so there's no network-policy dependency. Choose `git-repository` only
if you specifically want Snowflake to pull from your repo; your CI then runs
`snow git fetch` and Snowflake must reach GitHub (or you mint a GitHub-App token
into the secret).

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
