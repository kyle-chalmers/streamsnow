# Deploy setup

StreamSnow scaffolds a `.github/workflows/deploy.yml` that ships your apps to
Snowflake on merge to `main`. It **skips automatically** until you set the
secrets below, so it never fails a normal merge before you're ready.

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
it uploads `apps/` to the SHA-versioned stage and runs `CREATE OR REPLACE
STREAMLIT` (via `streamsnow deploy-sql`) for each app.

## git-repository note

The generated workflow targets the **stage-copy** path (CI pushes; Snowflake
never reaches out to GitHub — fewer moving parts, no network-policy dependency).
If you set `deploy.source: git-repository`, your CI must instead `snow git fetch`
the repository and Snowflake must be able to reach GitHub (or mint a GitHub-App
token into the secret). Use `streamsnow deploy-sql <slug>` for the create
statement and `streamsnow deploy-sql <slug> --refresh` for the
ABORT/PULL/COMMIT refresh of an existing app.
