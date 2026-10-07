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
| `USERADMIN` | `ci_role`, `viewer_role`, and a `TYPE = SERVICE` CI user with key-pair auth, no password (`RSA_PUBLIC_KEY` from `--public-key-file`, else a placeholder to paste) |
| `SECURITYADMIN` | both roles to `SYSADMIN`; the viewer role to **you** (whoever runs the script, via `CURRENT_USER()`) and to each `--viewer-user`; each existing `--viewer-role` gets it in a last section of its own; `USAGE` on the database, schema and warehouse to both roles; `CREATE STREAMLIT` + `CREATE STAGE` on the schema to `ci_role`; `USAGE` + `SELECT` on each allowed governance schema |
| `ACCOUNTADMIN` | container runtime: the PyPI external access integration (Snowflake's managed `snowflake.external_access.pypi_rule`) and `USAGE` on it and on the compute pool to `ci_role`; `CREATE COMPUTE POOL` only when your pool is not `SYSTEM_COMPUTE_POOL_CPU`, which Snowflake pre-provisions in every account; git-repository: the API integration |
| `ci_role` | the deploy-source objects it will own: the stage, or the secret + git repository |

```bash
streamsnow ci-key create                            # CI key pair + secret files, outside the repo
mkdir -p .internal                                  # run on its own, not chained with the next command
streamsnow deploy-setup --admin \
  --public-key-file ~/.streamsnow-ci/streamsnow_ci_rsa_key.pub > .internal/admin-setup.sql
snow sql -f .internal/admin-setup.sql -c <admin-connection>   # or run it in a Snowsight worksheet
```

With `--public-key-file` the script needs no edits (stage-copy source; a private
git repository still needs its GitHub token pasted in). Without it, paste the
public key over the `<paste public key>` placeholder. `--viewer-user NAME`
(repeatable) grants the viewer role to more people, and `--viewer-role ROLE`
(repeatable) grants it to a role you already use (an analyst or agent role), so
everyone holding that role can open the apps even when an admin login ran the
script. `PUBLIC`, the system roles and StreamSnow's own roles are refused.

With Claude Code, `/onboard` prepares this file for you (it runs `ci-key create` and
`deploy-setup --admin --public-key-file`, writing to `.internal/admin-setup.sql`, which git
ignores), copies it for Snowsight when you are the admin, or writes a note you can forward when
you are not. It never runs it.

**Safe to re-run.** Objects use `IF NOT EXISTS`, grants are idempotent, and the
CI user's key is re-applied with `ALTER USER`, so running the script again after
rotating the key, or just to check, changes nothing else. Snowflake has no
`IF NOT EXISTS` for `CREATE EXTERNAL ACCESS INTEGRATION`
([syntax](https://docs.snowflake.com/en/sql-reference/sql/create-external-access-integration)),
so the script uses `CREATE OR REPLACE` and re-grants `USAGE` to the CI role on
the next line. A deployed app keeps working when its integration is replaced:
tested live, it kept serving, and a redeploy against the replaced integration
passed `verify-deploy`. The cost is that grants or settings someone added to
the integration by hand are reset on each run, so manage it through this script.

Two things to check before running it:

- **Governance database grants.** The script grants `USAGE` plus `SELECT` on
  every object type an app can read, current and future, on each
  `governance.schema_allow` schema and never on a denied one: tables (hybrid
  tables included), views, dynamic tables, materialized views, semantic views,
  Iceberg tables and external tables. Each type needs its own grant; a grant on
  tables does not reach dynamic tables. Event tables, streams, functions and
  procedures are left out on purpose. Schema-level future grants replace
  database-level future grants of the same type for every role, so check
  `SHOW FUTURE GRANTS IN DATABASE <db>` before applying and repeat at schema
  level any that another role relies on. A **shared** database (a Marketplace or
  data-share import such as `SNOWFLAKE_SAMPLE_DATA`) does not take those
  grants; it needs `GRANT IMPORTED PRIVILEGES ON DATABASE ...` as
  `ACCOUNTADMIN`, which covers the whole share. The script emits that form
  automatically for `SNOWFLAKE_SAMPLE_DATA` and `SNOWFLAKE`, and as a commented
  alternative otherwise.
- **Viewer-role data grants are opt-in.** Deployed apps run with owner's rights
  (the CI role), so viewers only need `USAGE` on each app, and the script grants
  the viewer role no data access by default. The same data grants for the viewer
  role are printed commented out: uncomment them if you want local preview to
  connect as the viewer role and mirror what the deployed app reads, or preview
  with a developer role that has the CI role's reads.

### Starting fresh (`deploy-setup --teardown`)

To uninstall, or to rehearse a first setup on an account that already has one,
print the reverse:

```bash
streamsnow deploy-setup --teardown > teardown.sql   # review EVERY line, then run as ACCOUNTADMIN
```

It never runs anything itself. In order, it drops the app database (every
deployed app, plus the stage or git repository and secret inside it), the
warehouse, the CI service user, the viewer and CI roles, and then the
account-level objects: the PyPI external access integration, the configured
compute pool unless it is `SYSTEM_COMPUTE_POOL_CPU` (stopped first), and the git API integration
([DROP INTEGRATION](https://docs.snowflake.com/en/sql-reference/sql/drop-integration)).
Every statement uses `IF EXISTS`, so a partial teardown can be re-run. It keeps
your governance database and its data, and `SYSTEM_COMPUTE_POOL_CPU`, and it
refuses outright when `app_database` or `stage_database` is the governance
database or a Snowflake-shared one, or a role is a Snowflake system role. Two
things to check: every object that could predate StreamSnow (the database,
warehouse, integrations, a custom pool) carries a comment; delete that line if
other work uses it. And the CI
key pair under `~/.streamsnow-ci` stays, so the next `deploy-setup --admin
--public-key-file` re-registers the same key and your repo secrets keep working.

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
- **git-repository**: creates the API integration, the `GIT REPOSITORY`
  object and, for a private repo, the secret holding a GitHub token, and grants
  them to the CI role
  ([setting up Git](https://docs.snowflake.com/en/developer-guide/git/git-setting-up)).
  To preview it before switching, run
  `streamsnow deploy-setup --admin --source git-repository --git-origin <url>`;
  the full walkthrough is [Switching to the Git repository deploy source](git-repository.md).

## 2. CI auth (key-pair / JWT)

Create a key-pair for a dedicated CI **service user**, register the public key
on that user, and add these **repo secrets**. `streamsnow ci-key create` does
the first part and prepares the last: it uses `openssl` to write the same unencrypted
PKCS#8 key pair Snowflake's
[key-pair guide](https://docs.snowflake.com/en/user-guide/key-pair-auth) makes
and one file per secret below to `~/.streamsnow-ci` (`--dir` to change it),
reuses an existing key rather than replacing it, and prints only file names,
the key's `SHA256:` fingerprint (compare it with `RSA_PUBLIC_KEY_FP` in
`DESC USER`). The admin script registers the public key. Save a copy of the
private key (`streamsnow_ci_rsa_key.p8`) somewhere safe, such as a password
manager; if it is lost, make a new pair and re-run the admin script.

Then `streamsnow ci-key push` sets the five secrets from those files, each
value going straight to `gh secret set` on stdin, `SNOWFLAKE_ACCOUNT` last. It
prints only names. Prefer to do it by hand? From the repo, run
`gh secret set NAME < ~/.streamsnow-ci/secrets/NAME` for each of the five
names in this order: `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_RAW`,
`SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE`, then `SNOWFLAKE_ACCOUNT` (last, because
it switches the deploy job on). With Claude Code, `/onboard` runs `ci-key create` and, once
your admin has run the script, `ci-key push` for you.

Key-pair is the default because
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

The runner has no Snowflake CLI `config.toml`, so every `snow` call in the
workflow, and `streamsnow verify-deploy`, passes `--temporary-connection`: the
CLI then builds its connection from these `SNOWFLAKE_*` variables
([temporary connections](https://docs.snowflake.com/en/developer-guide/snowflake-cli/connecting/configure-connections#use-a-temporary-connection)).
Without the flag, Snowflake CLI 3.27 fails with `Connection default is not
configured`. The passphrase secret reaches `snow` as `PRIVATE_KEY_PASSPHRASE`,
the variable it reads for an encrypted key. A `deploy.yml` generated before
0.7.1 has neither; `streamsnow update --apply` re-renders it.

Once `SNOWFLAKE_ACCOUNT` is present, the deploy job runs on the next merge:
it uploads `apps/` to the SHA-versioned stage, runs `CREATE OR REPLACE
STREAMLIT` (via `streamsnow deploy-sql`) for each app, reconciles the
tombstone registry (`streamsnow check tombstones --drop-sql` — a
`DROP STREAMLIT IF EXISTS` per entry in `deploy/tombstones.yml`, idempotent
across re-runs, refusing if a tombstone still names a declared app), and
verifies each app's health (`streamsnow verify-deploy`). See
[Deploying → Retiring or renaming an app](deploying.md#retiring-or-renaming-an-app).

Note that an app's `sql_review/` directory (its runnable review SQL) is repo-side
documentation for reviewers — the deployed app never reads it, and it is not
declared in the app's `snowflake.yml` `artifacts:`.

## git-repository note

The generated workflow matches your `deploy.source`. The default **stage-copy**
rendering has CI push to a stage (Snowflake never reaches out to GitHub: fewer
moving parts, no network-policy dependency). With
`deploy.source: git-repository`, the rendered workflow instead runs
`snow git fetch`, so Snowflake must be able to reach GitHub, and then rebuilds
each app from the branch with `CREATE OR REPLACE STREAMLIT`. See
[Switching to the Git repository deploy source](git-repository.md).
