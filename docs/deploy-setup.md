# Deploy setup

StreamSnow scaffolds a `.github/workflows/deploy.yml` that ships your apps to
Snowflake on merge to `main`. It **skips automatically while the
`SNOWFLAKE_ACCOUNT` secret is unset** — that one secret is the gate, so it
never fails a normal merge before you're ready. Once it is set, the job runs,
and will fail at authentication if the other secrets below are still missing.

## 1. One-time Snowflake objects

Generate and review the DDL for your configured deploy source, then run it once
with an admin (or the CI) role:

```bash
streamsnow deploy-setup | less        # review first
streamsnow deploy-setup | snow sql --stdin   # or pipe to your admin session
```

- **stage-copy** (default): creates the internal stage CI uploads to. Container
  apps also need an account-level compute pool + external access integration
  (admin, one-time — emitted as commented guidance).
- **git-repository**: creates the API integration, the secret holding a GitHub
  token, and the `GIT REPOSITORY` object, and grants them to the CI role.

## 2. CI auth (key-pair / JWT)

Create a key-pair for a dedicated CI user, register the public key on that
Snowflake user, and add these **repo secrets**:

| Secret | Value |
|---|---|
| `SNOWFLAKE_ACCOUNT` | account locator (e.g. `ab12345.us-east-1`) |
| `SNOWFLAKE_USER` | CI service user |
| `SNOWFLAKE_PRIVATE_KEY_RAW` | the PEM private key |
| `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | (optional) key passphrase |
| `SNOWFLAKE_WAREHOUSE` | a warehouse the CI role can use |
| `SNOWFLAKE_ROLE` | your `ci_role` from `streamsnow.config.yaml` |

Once `SNOWFLAKE_ACCOUNT` is present, the deploy job runs on the next merge:
it uploads `apps/` to the SHA-versioned stage, runs `CREATE OR REPLACE
STREAMLIT` (via `streamsnow deploy-sql`) for each app, reconciles the
tombstone registry (`streamsnow check tombstones --drop-sql` — a
`DROP STREAMLIT IF EXISTS` per entry in `deploy/tombstones.yml`, idempotent
across re-runs, refusing if a tombstone still names a declared app), and
verifies each app's health (`streamsnow verify-deploy`). See
[Deploying → Retiring or renaming an app](deploying.md#retiring-or-renaming-an-app).

Note that an app's `sql_review/` audit-trail directory is repo-side
documentation for reviewers — the deployed app never reads it, and it is not
declared in the app's `snowflake.yml` `artifacts:`.

## git-repository note

The generated workflow matches your `deploy.source`. The default **stage-copy**
rendering has CI push to a stage (Snowflake never reaches out to GitHub — fewer
moving parts, no network-policy dependency). With
`deploy.source: git-repository`, the rendered workflow instead runs
`snow git fetch` and Snowflake must be able to reach GitHub (or mint a
GitHub-App token into the secret). Use `streamsnow deploy-sql <slug>` for the create
statement and `streamsnow deploy-sql <slug> --refresh` for the
ABORT/PULL/COMMIT refresh of an existing app.
