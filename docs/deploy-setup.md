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
  (admin, one-time — emitted as commented guidance). Sizing note from the
  [compute pool docs](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/working-with-compute-pool):
  the pre-provisioned `SYSTEM_COMPUTE_POOL_CPU` packs three apps per node,
  while a pool you create runs **one app per node**, so size `MIN_NODES` to the
  apps you expect running at once. A container app's server keeps running until
  three days pass with no viewer ([billing](https://docs.snowflake.com/en/developer-guide/streamlit/object-management/billing)).
  PyPI access: Snowflake now prefers a **Snowflake artifact repository** over an
  external access integration, and configuring both disables the EAI
  ([external access](https://docs.snowflake.com/en/developer-guide/streamlit/features/external-access)).
  StreamSnow still emits the EAI because the Snowflake CLI's `snowflake.yml`
  schema has no artifact-repository field yet; if your account already uses one,
  do not also attach the EAI.
- **git-repository**: creates the API integration, the secret holding a GitHub
  token, and the `GIT REPOSITORY` object, and grants them to the CI role
  ([setting up Git](https://docs.snowflake.com/en/developer-guide/git/git-setting-up)).

## 2. CI auth (key-pair / JWT)

Create a key-pair for a dedicated CI **service user**, register the public key
on that user, and add these **repo secrets**. Key-pair is the default because
Snowflake's [MFA rollout](https://docs.snowflake.com/en/user-guide/security-mfa-rollout)
blocks password authentication for service users in its final phase (Aug–Oct
2026, account-specific and subject to change); a
[programmatic access token](https://docs.snowflake.com/en/user-guide/programmatic-access-tokens)
or the CLI's workload-identity authenticator are the supported alternatives if
your platform team prefers them (edit the `SNOWFLAKE_AUTHENTICATOR` line in the
generated workflow). Never a password.

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
