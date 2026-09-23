# Deploy setup

StreamSnow scaffolds a `.github/workflows/deploy.yml` that ships your apps to
Snowflake on merge to `main`. It **skips automatically while the
`SNOWFLAKE_ACCOUNT` secret is unset** — that one secret is the gate, so it
never fails a normal merge before you're ready. Once it is set, the job runs,
and will fail at authentication if the other secrets below are still missing.

## 0. The admin bootstrap (`deploy-setup --admin`)

A first deploy needs objects most people cannot create themselves: the app
database and schema, a warehouse, a CI role and a viewer role, a CI service
user, the grants that tie them together, and (container runtime) a PyPI
external access integration and compute pool access. `streamsnow deploy-setup
--admin` prints all of it from your `streamsnow.config.yaml`, as one reviewable
script split into `USE ROLE` sections, each run by the narrowest system role
that can:

| Section | Creates or grants |
|---|---|
| `SYSADMIN` | app database + schema (and the stage schema if different), an `XSMALL` warehouse (`AUTO_SUSPEND = 60`, `INITIALLY_SUSPENDED`) |
| `USERADMIN` | `ci_role`, `viewer_role`, and a `TYPE = SERVICE` CI user with an `RSA_PUBLIC_KEY` placeholder (key-pair auth, no password) |
| `SECURITYADMIN` | both roles to `SYSADMIN`; `USAGE` on the database, schema and warehouse to both roles; `CREATE STREAMLIT` + `CREATE STAGE` on the schema to `ci_role`; `USAGE` + `SELECT` on each allowed governance schema |
| `ACCOUNTADMIN` | container runtime: the PyPI external access integration (Snowflake's managed `snowflake.external_access.pypi_rule`) and `USAGE` on it and on the compute pool to `ci_role`; `CREATE COMPUTE POOL` only when your pool is not `SYSTEM_COMPUTE_POOL_CPU`, which Snowflake pre-provisions in every account; git-repository: the API integration |
| `ci_role` | the deploy-source objects it will own: the stage, or the secret + git repository |

```bash
streamsnow deploy-setup --admin > admin-setup.sql   # review, paste in the public key
snow sql -f admin-setup.sql -c <admin-connection>   # or run it in a Snowsight worksheet
```

Two things to check before running it:

- **Governance database grants.** The script grants `USAGE` + `SELECT` (current
  and future tables and views) on each `governance.schema_allow` schema, never
  on a denied one. A **shared** database (a Marketplace or data-share import
  such as `SNOWFLAKE_SAMPLE_DATA`) does not take those grants; it needs
  `GRANT IMPORTED PRIVILEGES ON DATABASE ...` as `ACCOUNTADMIN`, which covers
  the whole share. The script emits that form automatically for
  `SNOWFLAKE_SAMPLE_DATA` and as a commented alternative otherwise.
- **Viewer-role data grants.** Deployed apps run with owner's rights (the CI
  role), so viewers only need `USAGE` on each app. The viewer role also gets the
  data grants because local preview connects as that role; drop those lines if
  viewers must never query the data directly.

## 1. One-time Snowflake objects

If an admin already ran the bootstrap above, you are done with this step (its
last section is this output). Otherwise, generate and review the DDL for your
configured deploy source, then run it once with an admin (or the CI) role:

```bash
streamsnow deploy-setup | less        # review first
streamsnow deploy-setup | snow sql --stdin   # or pipe to your admin session
```

- **stage-copy** (default): creates the internal stage CI uploads to. Container
  apps also need an account-level compute pool + external access integration
  (admin, one-time: emitted as commented guidance here, as real DDL by
  `--admin`). `SYSTEM_COMPUTE_POOL_CPU` already exists in every account, with
  `USAGE` granted to `PUBLIC` by default
  ([compute pools](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/working-with-compute-pool)),
  so it is never created. Sizing note from the
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
