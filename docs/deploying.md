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

1. Authenticates to Snowflake as your `ci_role` (key-pair / JWT) from the
   `SNOWFLAKE_*` secrets. Every `snow` call, and `verify-deploy`, passes
   `--temporary-connection`, because the runner has no Snowflake CLI
   `config.toml` (see [Deploy setup](deploy-setup.md#2-ci-auth-key-pair--jwt)).
2. Makes the app source available to Snowflake — how depends on your
   **[deploy source](#two-deploy-sources)**.
3. **Builds app data.** `streamsnow objects-sql` prints every view and dynamic
   table the apps declare in `governance.app_data`, in dependency order across
   apps, and the job runs it once before any app. Any finding prints nothing and
   stops the deploy. The job writes every deploy file into a private work
   directory before its first `snow` call, and skips this step when no app
   declares an object. With the git-repository source,
   `streamsnow git-head --expect $GITHUB_SHA` runs after the fetch and stops the
   deploy when another merge moved the branch, because the apps would then come
   from a newer commit than the app data.
4. Runs `CREATE OR REPLACE STREAMLIT` for each app under `apps/` via
   `streamsnow deploy-sql`.
5. **Reconciles tombstones**: drops every retired identifier listed in
   `deploy/tombstones.yml` (see
   [Retiring or renaming an app](#retiring-or-renaming-an-app)).
6. **Verifies deploy health** per app (`streamsnow verify-deploy`): object
   exists, live version set, no container crash-loop signature. With the
   **stage-copy** source it also confirms the version source matches the merge
   SHA. The **git-repository** workflow passes the merge SHA too, and the
   check compares it with the `last_version_git_commit_hash` that
   `DESCRIBE STREAMLIT` reports, because a Git-sourced app is built from the
   branch rather than a commit path.
   Existence comes from `SHOW STREAMLITS`; the live-version and version-source
   checks read `DESCRIBE STREAMLIT`, the only one of the two that carries the
   version URIs. A check that cannot run prints `○ <check> (skipped)` with the
   reason, and the summary line counts it apart from the passes
   (`PASS: store-sales (3 passed; 1 skipped: service-logs)`). Skips never fail
   the run; the container service-log scan is best-effort and often skips.
   With the **stage-copy** source, `verify-deploy` also lists the staged files
   for the commit (`stage-files`): it warns for each file `stage-bundle` would
   ship for the app that is missing from the stage, and for each staged file
   the deploy bundle leaves out (see [What the stage-copy upload ships](#what-the-stage-copy-upload-ships)).
   It is warn-only: it prints `! stage-files (warning)` and counts
   `1 warned: stage-files` in the summary, and the run still passes.

The scaffolded checks workflow runs `validate-app` on every PR, but nothing
wires it to the deploy job: `checks.yml` and `deploy.yml` are independent
workflows, and StreamSnow does not scaffold branch protection. The gate holds
only if your repo requires the checks before merge — recommended: add a GitHub
branch protection rule (or ruleset) on `main` that lists the checks jobs as
required status checks. With that in place, the deploy job can safely assume
merged code already passed.

An app's `sql_review/` directory (one runnable SQL file per page, which
`streamsnow sql-review` generates from its `index.yaml`) is a **repo-side
artifact for human reviewers** — the running app never reads it, and the
scaffolded `snowflake.yml` does not declare it among the app's `artifacts:`. It
exists so someone can re-run each visual's SQL in DataGrip or Snowsight, not to
ship. The app's `review.py` (the no-op `review_value` marker its pages import)
does ship, and is in `artifacts:`.

Before a release or a deploy that changes an app's SQL, run `/sql-review <slug>`:
it checks the app's objects, grants and sections against live Snowflake and
commits a review log under `sql_review/review_log/` for a person to sign off.
It is recommended, never required: nothing in the deploy path waits on it.

## What the stage-copy upload ships

The stage-copy workflow does not upload the `apps/` tree as it sits in the
repo. It first runs `streamsnow stage-bundle --out "$RUNNER_TEMP/ss-bundle"`,
which copies each app into a bundle without the files the running app never
reads, then uploads the bundle to `@<stage>/commits/<sha>/apps/`. The bundle
leaves out:

- root-level `*.md` files (`AGENTS.md`, `CLAUDE.md`, `REQUIREMENTS.md`,
  `README.md`), unless an `artifacts:` entry in the app's `snowflake.yml`
  declares one, as an app that renders its own `help.md` would;
- `sql_review/`, including `review_log/`;
- dot-directories other than `.streamlit`, `__pycache__`, and `*.egg-info` directories
  (build metadata from a local editable install, often ignored by version control), plus
  `*.pyc` and `.DS_Store` files;
- everything in `.streamlit/` except `config.toml`, so a local `secrets.toml`
  never ships;
- `.env` and `.env.*` files anywhere in the app, even when declared, because
  Streamlit in Snowflake never reads one and a committed one usually holds
  credentials;
- any file named `secrets.toml`, at any depth and under any link, because
  Streamlit in Snowflake does not read it and a local copy holds connection
  credentials;
- symlinks whose target is itself left out, judged at the path the target has
  in the repo (a `runtime.txt` link to `../../shared/.env` stays out with
  `.env`).

Symlinks that resolve anywhere inside the repo are followed, as
`snow stage copy apps/ --recursive` did. An app can share a helper with
`apps/acme/helpers.py -> ../../shared/helpers.py`, or a theme with a link to a
shared `.streamlit/config.toml`, and `.streamlit -> config/` still ships
`.streamlit/config.toml`. A symlink that resolves outside the repo fails the
bundle with exit 2 and a message naming the link, before anything is written,
so the deploy stops before the upload instead of shipping an app that is
missing a file. Move the target into the repo or replace the link with a copy.
The repo is the `--dir` folder (default: the current directory).

`streamsnow stage-bundle --out <empty dir>` prints each file it left out and
why, so you can run it locally to see what a deploy will upload. A repo whose
`deploy.yml` predates the bundle still uploads all of `apps/`; the
`stage-files` warning in `verify-deploy` names the files that should not be
there, and `streamsnow update --apply` re-renders the workflow. Files already
staged under earlier commits stay there until you clean the stage.

The **git-repository** source cannot use the bundle: Snowflake builds each app
from the committed repo folder, so committed docs are part of what it reads.
See [Switching to the Git repository deploy source](git-repository.md#should-you-switch).

## Retiring or renaming an app

The pipeline above only ever runs `CREATE OR REPLACE` — it has **no implicit
delete path**. Renaming `apps/<a>/` to `apps/<b>/` mints a *new* object with a
*new* URL; removing a directory just stops re-deploying the old object. Either
way, the previously deployed STREAMLIT lives on, frozen at the last merge that
deployed it. Nothing after the merge notices: `verify-deploy` only checks the
app directories that still exist, so the PR-time tombstone check below is the
only thing that catches it.

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

### App-data objects

A view or dynamic table in `governance.app_data` is retired the same way, with
one addition: the tombstone carries `kind: view` or `kind: dynamic_table`, and
`--drop-sql` emits `DROP VIEW IF EXISTS` or `DROP DYNAMIC TABLE IF EXISTS` by
that kind (never `DROP STREAMLIT` for a name in app data). A tombstone with a
kind may only name an object in the current `governance.app_data`; objects
left behind in an old schema after `app_data` moves are dropped by hand. A
kindless tombstone for an app-data name is refused, and an inventory the check
cannot read completely fails closed. Changing an object's kind (view to
dynamic table) needs a new name: tombstone the old one. `deploy-setup
--teardown` drops declared app-data objects, dependents first, before the role
drops, when app data lives outside the app database.

## Two deploy sources

Set `deploy.source` in `streamsnow.config.yaml`:

| | **stage-copy** (default) | **git-repository** |
|---|---|---|
| Mechanism | CI uploads `apps/` to a SHA-versioned internal stage; the STREAMLIT serves `FROM '@stage/commits/<sha>/...'` | CI runs `snow git fetch`, then each app is rebuilt `FROM '@<repo>/branches/<branch>/...'` |
| Network direction | CI → Snowflake only | Snowflake → GitHub (must be reachable) |
| One-time objects | an internal stage | API integration (ACCOUNTADMIN) + `GIT REPOSITORY`, plus a secret holding a GitHub token for a private repo |
| Best when | you want the fewest moving parts and no Snowflake→GitHub dependency | you want the repo browsable in Snowsight and can give Snowflake access to GitHub |
| Limits | stage retains every SHA (your rollback surface) | repositories over 2 GB are unsupported ([Git overview](https://docs.snowflake.com/en/developer-guide/git/git-overview)) |

The scaffold renders `deploy.yml` for whichever source your config declares.
With the default **stage-copy**, Snowflake never reaches out to GitHub, so
there's no network-policy dependency. To move to `git-repository`, follow
**[Switching to the Git repository deploy source](git-repository.md)**;
`streamsnow deploy-setup --admin --source git-repository` previews its setup
SQL without changing your config.

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
  The wizard defaults `compute_pool` to `SYSTEM_COMPUTE_POOL_CPU`, which already
  exists in every account, so only the integration is new.
  **Warehouse** apps need neither.
- **git-repository**: creates the API integration, the `GIT REPOSITORY` object
  and, for a private repo, the secret holding a GitHub token, and grants them
  to your `ci_role` ([guide](git-repository.md)).

Then add the CI auth secrets (key-pair / JWT for the CI user). The full secret
table is in **[Deploy setup → CI auth](deploy-setup.md#2-ci-auth-key-pair--jwt)**.
Once `SNOWFLAKE_ACCOUNT` is present, the deploy job runs on the next merge.

## The per-app create statement

`streamsnow deploy-sql` emits the SQL the workflow runs — useful to inspect or
to deploy a single app by hand:

```bash
streamsnow deploy-sql <slug>                 # CREATE OR REPLACE STREAMLIT (stage-copy embeds the SHA)
streamsnow deploy-sql <slug> --sha <sha>     # pin a specific commit (stage-copy)
```

`streamsnow stage-path` prints the stage base path (`@DB.SCHEMA.STAGE`) the
stage-copy upload targets, and `streamsnow stage-bundle --out <dir>` builds the
per-app bundle that upload copies.

## Runtime notes

- **Container** (`runtime: container`, default): the app's `snowflake.yml`
  declares `runtime_name`, `compute_pool`, and `external_access_integrations`;
  dependencies come from `pyproject.toml`. The compute pool + EAI must exist
  before the first deploy (see one-time setup).
- **Warehouse** (`runtime: warehouse`): no compute pool or EAI; dependencies come
  from `environment.yml` (Snowflake Anaconda channel). Never pin `python` there —
  a pinned interpreter failed `CREATE STREAMLIT` in production with
  `Packages not found: python==3.11`, so `validate-app` rejects the line. The
  official [dependency management](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/dependency-management)
  example does show `python=3.11`; StreamSnow keeps the stricter rule until the
  single-`=` form is re-tested against a live app (tracked in the CHANGELOG's
  Known gaps).
- **Deploying by hand** with `snow streamlit deploy` instead of the generated
  workflow? Container apps need Snowflake CLI 3.14 or newer for that command
  ([snow streamlit deploy](https://docs.snowflake.com/en/developer-guide/snowflake-cli/command-reference/streamlit-commands/deploy));
  the workflow itself emits `CREATE STREAMLIT` through `snow sql` and has no
  such floor.

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

When an app owns a dynamic table, `verify-deploy` also runs the warn-only
`app-data-refresh` check: it warns when the table's `scheduling_state` is not
`RUNNING` or `ACTIVE`, when it has never refreshed, or when it is not found,
and reports it as skipped when the query fails.

## Re-rendering the pipeline after a config change

If you change deploy-related config (source, warehouse, roles), re-render the
generated governance files:

```bash
streamsnow update            # dry-run: shows what would change
streamsnow update --apply    # write the changes
```

`update` re-renders `AGENTS.md`, `CLAUDE.md`, hooks, CI, and `deploy.yml` from your current
config; it leaves `README` and `.gitignore` alone. Run it after upgrading
`streamsnow` across a minor version too — 0.7 moved the generated CI pin to
`streamsnow>=0.7,<0.8` and added the `sql_review.coverage` policy, and only a
re-render picks those up. 0.7.1 is the same: its `deploy.yml` passes
`--temporary-connection` to every `snow` call and to `verify-deploy`, and
installs `streamsnow>=0.7.1,<0.8`, the first release with that
`verify-deploy` option. 0.7.2's `deploy.yml` skips the deploy and verify
steps cleanly while `apps/` holds no app directory, and installs
`streamsnow>=0.7.2,<0.8`, the first release whose `verify-deploy` reads
`DESCRIBE STREAMLIT` (an older one reports those checks as passed without
running them). 0.8 moves every generated pin to `streamsnow>=0.8,<0.9`, adds
the repo's `.sqlfluff` (created when missing, never overwritten) and the
`sql-review check --lint-files` pre-commit hook; apps still need their
`sql_review/` moved to the new format by hand
([Auditing a visual](auditing-a-visual.md#for-the-developer-on-the-other-side-of-this)).
0.9 moves every generated pin to `streamsnow>=0.9,<0.10`, and 0.10 to
`streamsnow>=0.10.1,<0.11`.

## See also

- [Deploy setup](deploy-setup.md) — one-time Snowflake objects + CI secret list.
- [Getting started](getting-started.md) — scaffold, preview, validate.
- [Data discovery](data-discovery.md) — wire governed queries before you ship.
