# Changelog

All notable changes to StreamSnow are recorded here. This project follows
[semantic versioning](https://semver.org/): from 1.0.0 the stable surface in
[docs/versioning.md](docs/versioning.md) breaks only in a major release, after a deprecation
period. Before 1.0, a breaking change can land in any minor release and is called out in its
entry.

## [Unreleased]

### Added

- **Versioning and stability policy** ([docs/versioning.md](docs/versioning.md)): what the
  stable surface is, why a stricter check counts as a breaking change (warn-only for one
  minor release first), the deprecation window that applies from 1.0.0, and the path to 1.0.0. A new
  `tests/test_cli_surface.py` pins every command, flag and argument in
  `tests/fixtures/cli_surface.json`, so a rename or removal fails CI as BREAKING. The
  release gates for 1.0.0 are in `RELEASING.md`.
- **Community intake and agent-run maintenance** for this repository: issue forms (blank
  issues off), a PR template with AI-use disclosure, labels as code, CODEOWNERS, Dependabot,
  a Code of Conduct, `SUPPORT.md`, `.claude/CLAUDE.md` for agents, and workflows that triage every
  issue (the model only classifies; scripts apply labels and canned comments), implement
  maintainer-approved `ready-for-agent` issues as PRs, review same-repo PRs, answer
  maintainer `@claude` comments, post a daily maintainer digest, and close issues left in
  `status:needs-info` for 14 days. CONTRIBUTING describes the flow and the AI-assisted
  contribution policy.

## [0.7.7] - 2026-10-05

### Added

- **`streamsnow ci-key push`** sets the five deploy secrets on GitHub from the
  files `ci-key create` wrote. Each value goes to `gh secret set` on stdin,
  never on the command line, and only names are printed. `SNOWFLAKE_ACCOUNT`
  goes last, and a failure stops before it, so deploys never switch on with a
  partial set. It checks `gh`, its sign-in, the target repo and every file
  before setting anything.
- **Key guard hook** (`hooks/secret_guard.py`, launched through the cross-shell
  `uv run` launcher). A `PreToolUse` guard that denies Claude's shell, file and
  search tools any access to `~/.streamsnow-ci`, apart from `streamsnow ci-key
  ...` and `streamsnow deploy-setup ...`. File and search tools are checked on
  the location they touch, not the text they write or search for, so docs that
  mention the directory stay editable. Covers PowerShell and Windows paths.
  Not repo-gated, and it denies rather than asks.
- **README: "What Claude can and can't see"**, before the install section, with
  a secrets-flow diagram, and a matching "How StreamSnow handles secrets"
  section in SECURITY.md. A test checks the README still names `ci-key push` and the guard.

### Changed

- **The plugin's Python hooks launch through `uv`, not `python3`.** The deploy
  guard and the review gate now run as `uv run --no-project --offline
  --no-python-downloads ... ; exit 0`. That one command works in every shell
  Claude Code runs hooks in (sh, Git Bash, and PowerShell on Windows without
  Git Bash), needs no `python3` on PATH (Windows often has none, or only the
  Microsoft Store stub), never touches your project's environment or the
  network, and exits 0 whatever goes wrong, so a broken launcher can never
  block your commands. uv is already a required prerequisite; if it is
  missing from PATH the hooks are silently off, and `streamsnow doctor` flags
  the missing uv. The guard costs about 0.1s more per shell command.
- **The deploy guard watches PowerShell too.** On Windows the PowerShell tool
  is Claude's default shell, so the guard now inspects it as well as Bash, and
  catches the Windows shapes of the same commands: `snow.exe`, backslash
  paths, the `&` call operator, PowerShell's backtick escape, and SQL piped in
  with `Get-Content deploy.sql | snow sql --stdin`.
- **`streamsnow preview` manages the process on native Windows.** Start,
  status and stop now work there (psutil, installed on Windows only), and stop
  takes the whole process tree, since `streamlit.exe` is a launcher whose
  child process holds the port.
- `ci-key create`'s closing steps point at `ci-key push` instead of a
  `gh secret set` loop, and remind you to save a copy of the private key
  somewhere safe, such as a password manager. docs/deploy-setup.md keeps the manual loop for anyone
  who prefers it.
- **`/start-app --setup` sets the deploy secrets with `ci-key push`.** Claude
  runs `streamsnow ci-key create` and, once your admin has run the admin script,
  `streamsnow ci-key push`, instead of setting values with `gh secret set
  --body` and handing you the private key command.

### Fixed

- **Text encoding and line endings on Windows.** Python on Windows reads and
  writes text as cp1252 by default, and Git for Windows checks files out with
  CRLF. `streamsnow init` and `new` crashed writing the scaffold's emoji, and
  `validate-app`, `doctor` and `--help` crashed when their output went to a
  pipe. Every file read and write now names UTF-8, a test fails any that does
  not, and the CLI switches a non-UTF-8 stdout to UTF-8. `sql-review` writes LF
  and hashes CRLF pairs as LF, so a Windows checkout no longer reads every
  committed review file as hand-edited or its inputs as drifted (a lone CR, or
  any other byte change, still counts as an edit). The cached Anaconda package
  list no longer crashes on Windows, the review gate keeps its state in the
  system temp dir instead of `/tmp`, and findings print paths with forward
  slashes. CI now runs the tests on Windows and macOS as well as Linux. The
  `/start-app --setup` routing to WSL stays until local preview and the hooks
  work natively.
- **`streamsnow preview start` behind a proxy.** The health probe of
  `127.0.0.1` honored `HTTP_PROXY` and the macOS/Windows system proxy, so on a
  machine whose proxy does not exempt localhost a serving app read as "not
  healthy" after the full timeout. The probe now always connects directly.
  `ci-key create` no longer warns on Windows that the key directory is
  readable by other users (Windows reports every directory as 0o777 and
  protects it with ACLs instead).

## [0.7.6] - 2026-10-04

### Added

- **The plugin bundles the Playwright MCP** (`.mcp.json`, an exact
  `@playwright/mcp` pin). Five skills already walked running apps in a browser,
  but nothing set the browser tool up, so the walkthrough was silently skipped
  for anyone who had not configured one by hand.
- **Onboarding rows in `streamsnow doctor`:** `repo-files`, `git-identity`
  (reports only whether name and email are set) and `pre-commit-hook` (pre-commit's
  own hook, honoring `core.hooksPath`), all required once a config exists;
  `node` (warning) for the browser tool; `ci-secrets`, which reads secret names
  through `gh` and says "not checked", never "missing", when it cannot list them.
  On native Windows a `platform` row points to WSL.
- **`/start-app --setup` runs the setup and explains each step.** Before every
  install it says in a line or two what it is and why the user needs it, then
  waits for a yes. It routes a teammate cloning a configured repo straight to
  machine setup, adds a browser check, saves the plugin at project scope, and
  sets the deploy workflow's non-secret GitHub secrets (the user sets the key).
- **`streamsnow deploy-setup --teardown` prints the start-fresh reverse of
  `--admin`.** Reviewable DROP statements, never run: the app database (and
  every deployed app in it), warehouse, CI service user, viewer and CI roles,
  then the external access integration, the configured compute pool (never
  `SYSTEM_COMPUTE_POOL_CPU`), and the git API integration. Every DROP uses
  `IF EXISTS`. It keeps the governance database, refuses when the app or stage database is the governance
  database or a Snowflake-shared one or a role is a system role, and marks each
  object that could predate StreamSnow.
- **`streamsnow ci-key create` makes the CI key pair and the five secret
  files.** It writes them to `~/.streamsnow-ci` with `openssl`, reuses an
  existing key, never overwrites a secret file, and prints only file names,
  the `SHA256:` fingerprint `DESC USER` shows, and the `gh secret set` loop.
- **`deploy-setup --admin --public-key-file` fills in the CI user's key**, so
  the stage-copy admin script runs unedited, and re-applies it with
  `ALTER USER` on a re-run. `--viewer-user NAME` (repeatable) grants the
  viewer role to more users.

### Changed

- **The admin script is safe to re-run end to end.** The PyPI external access
  integration, which Snowflake cannot create with `IF NOT EXISTS`, now uses
  `CREATE OR REPLACE` followed by the CI role's `USAGE` grant. A deployed app
  kept serving through a replace in a live test.
- **The admin script grants the viewer role to whoever runs it**, through
  `CURRENT_USER()`, instead of leaving a commented `GRANT` to fill in.

### Fixed

- `docs/getting-started.md` promised a git-identity check that doctor did not
  have.

## [0.7.5] - 2026-10-03

### Added

- **`init` and `configure` take the five wizard answers as flags.**
  `--runtime`, `--account` (or `--connection <name>`), `--database`,
  `--schemas` and `--deploy-source` each replace their question, and
  `--deny-schemas` sets `governance.schema_deny` (default `RAW,STAGING`; `''`
  denies none). With all five passed no prompt fires. The flags go through the
  same defaults and prefill logic as the wizard, so the same answers write the
  same file. `--connection` reads the account from that `snow` connection
  without printing it and uses the connection as `snowflake.connection_name`.
  Bad values, `--account` with `--connection`, a schema both allowed and
  denied, or flags combined with `--config` exit 2 before anything is written;
  on an existing config the flags need `--reconfigure`, so they are never
  silently ignored. The interactive wizard is unchanged.

### Changed

- **`/start-app --setup` proposes the wizard's answers.** Instead of leaving
  the user to answer the wizard cold, the setup skill first works out what
  Snowflake access the user has (a default `snow` connection, other
  connections, a Snowflake MCP server, a dbt profile, or nothing) and offers
  to help create a `snow` connection when there is none, in the user's own
  terminal so the account is never typed into chat. It then probes the
  account read-only (`SHOW` with `INFORMATION_SCHEMA` fallbacks for tools that
  refuse it), checks the role before trusting an empty result, and weighs the
  evidence as a method rather than name rules: what the user said, then
  comments, then what objects hold, then dbt, with names last. It asks only
  what the evidence could not settle, writes the config with `configure` and
  the confirmed answers, lets the user change the unasked defaults
  (warehouse, roles, app database, compute pool), then runs
  `streamsnow init --no-starter-app` so the governed files render from the
  final values. Everything is a proposal the user can skip or override,
  including `container` or `git-repository` against the evidence and keeping
  a non-default connection (step 3 then sets up preview for it). It never
  runs DDL or grants and never handles credentials in chat.

## [0.7.4] - 2026-10-03

The git-repository deploy source, hardened from a live test against Snowflake
on 2026-10-03. Stage-copy stays the default.

### Fixed

- **A git-repository deploy could report success while serving old code.**
  The workflow refreshed existing apps with ABORT / PULL / COMMIT behind
  `|| true`, and verify-deploy skipped the version check for this source.
  Deploys now run `CREATE OR REPLACE STREAMLIT ... FROM
  '@<repo>/branches/<branch>/apps/<slug>/'` after `snow git fetch`, the same
  idempotent statement stage-copy uses; a failed fetch fails the deploy.
  Snowflake rejects a `/commits/<sha>/` path as a Git source ("Invalid git
  branch path"), so `verify-deploy --sha` now compares the merge commit with
  the `last_version_git_commit_hash` that `DESCRIBE STREAMLIT` reports. The
  generated workflow passes `--sha "$GITHUB_SHA"` to it. Re-render with
  `streamsnow update --apply` to pick this up; an older workflow's
  `deploy-sql --refresh` call still works (it now prints a comment).
- **The git setup SQL needed hand edits.** `CREATE GIT REPOSITORY` carried a
  literal `<https://github.com/your-org/your-repo.git>` placeholder, and the
  API integration allowed every GitHub repository. The new
  `deploy.git_origin` config field fills `ORIGIN`, the integration allows only
  `https://github.com/<owner>`, the non-admin script grants the CI role the
  WRITE it needs to fetch, and the generated workflow is no longer labeled
  experimental.

### Added

- **`github_auth_mode: public`.** A public repo needs no token: no secret, no
  `GIT_CREDENTIALS`, no `CREATE SECRET` grant.
- **`streamsnow deploy-setup --source git-repository`** prints the other deploy
  source's setup for review without changing your config (with `--git-origin`
  and `--github-auth`), under a `PREVIEW` banner. It never runs SQL.
- **[Switching to the Git repository deploy source](docs/git-repository.md)**:
  when to choose it, a least-privilege GitHub token, switching the config,
  checking the first deploy, and dropping the old stage.

## [0.7.3] - 2026-10-03

### Removed

- **The 8 pre-0.3 alias commands.** `/new-app`, `/refine-requirements`,
  `/add-page`, `/onboard`, `/auto-review-app`, `/sql-review`, `/apply-review`
  and `/deep-dive-data` only forwarded to the 8 real skills, and they doubled
  the `/streamsnow:` menu to 16 entries. The `commands/` folder is gone, so the
  menu now lists the 8 skills. Use `/start-app` (with `--spec` or `--setup`),
  `/review-app` (with `--fix`, `--auto` or `--sql`) and `/audit-lineage`
  instead; [docs/migrating-a-consumer-repo.md](docs/migrating-a-consumer-repo.md)
  maps each old name. This was promised for the next major release; it ships
  in a patch because the plugin is still beta and the aliases added nothing
  the skills do not. The CLI is unchanged.

### Changed

- **Install docs use project scope and `/reload-plugins`.** The README's agent
  install prompt, Quickstart and upgrade steps install the plugin with
  `--scope project` and load it with `/reload-plugins` instead of a restart.
  The README also gains a diagram of how the skills fit together.

## [0.7.2] - 2026-09-27

Fixes from the first real GitHub Actions deploy and from a `doctor` run on a
machine that had been idle.

### Fixed

- **`verify-deploy` reported checks it never ran as passed.** Current
  Snowflake's `SHOW STREAMLITS` returns no version URIs, so on the first real
  CI deploy the live-version and version-source checks each printed a check
  mark above "not in SHOW output, skipped". `verify-deploy` still uses
  `SHOW STREAMLITS` to confirm the app exists, then reads `DESCRIBE STREAMLIT`
  for the other two: live-version fails when `live_version_location_uri` is
  empty, and version-source (stage-copy) fails when neither
  `default_version_source_location_uri` nor `last_version_source_location_uri`
  contains `/commits/<sha>/`. A check that cannot run now prints
  `○ <check> (skipped)` with the reason, reports `"status": "skipped"` in
  `--format json`, and is counted apart from the passes, as in
  `PASS: store-sales (3 passed; 1 skipped: service-logs)`. Skips still never
  fail the run, and the container service-log scan stays best-effort. Existing
  deploy workflows get this on their next run without a re-render: their
  `streamsnow>=0.7.1,<0.8` pin resolves to the newest 0.7 release.
- **A repo with deploy secrets but no app failed its deploy.**
  `streamsnow init --no-starter-app` writes no `apps/` directory, so
  `snow stage copy "apps/"` stopped the deploy with "No data" (exit 2), and
  `for d in apps/*/` ran once over the literal pattern. Both deploy workflows
  now collect app directories with `nullglob`; when there are none, the deploy
  and verify steps print "No app directories under apps/ yet: nothing to
  deploy" (or "verify") and exit 0 before any `snow` call. Existing repos
  re-render `deploy.yml` with `streamsnow update --apply` and commit it.
- **`doctor` called a slow `snow` broken.** The first `snow --version` after a
  Mac sat idle took 24.8 s on about 1 s of CPU, past the 15 s probe timeout, so
  doctor printed `[BROKEN ] snow` with reinstall advice and then skipped the
  connection checks. The `snow` probes now wait 45 s. A timeout is a warning to
  re-run doctor, never a failure, and the connection listing still runs once
  afterwards. If that listing also fails or times out, `snow-connection` and
  `snow-key-file` say "not checked" with the reason instead of suggesting
  `snow connection add`. A `snow` that exits non-zero, such as an import crash,
  is still `BROKEN` with the `uv tool install snowflake-cli` hint.

### Changed

- **The deploy workflows install `streamsnow>=0.7.2,<0.8`** (was `>=0.7.1`),
  the first release whose `verify-deploy` actually evaluates the live-version
  and version-source checks. `streamsnow update --apply` writes the new pin
  along with the no-apps guard.

## [0.7.1] - 2026-09-27

The launch-fix release. An end-to-end run of the install path from an empty
folder (PyPI package plus the marketplace plugin) found that the build half
worked and the ship half did not: the plugin setup path never wrote the
governed repo files, a fresh scaffold failed its own CI, and nothing created
the Snowflake objects a first deploy needs. A second pass, building a
dashboard over a historical sample dataset with a key-pair connection, found
the rest: empty review SQL and empty default dashboards on data that ends in
the past, a local preview crash, and a starter app that could still ship.

### Added

- **`streamsnow agent-skills install --agent codex`** (and `list`): copies the
  skills and their shared recipes into `.agents/skills` (repo scope, committed)
  or `~/.agents/skills` (user scope), where OpenAI Codex CLI finds them
  (tested with 0.157.1). A manifest keeps a re-run from overwriting edited
  skills; `ship-app` and `migrate-app` stay explicit-only in Codex, as in
  Claude Code. The wheel now ships the skills (`streamsnow/_skills`), `init`'s
  `Next:` block names the Codex install, and `skills/_shared/other-agents.md`
  covers what reads differently outside Claude Code (skill syntax, subagents,
  checkpoints, and the plugin hooks, which have no Codex equivalent). README:
  "Use with other agents".
- **`streamsnow init --no-starter-app`**: the config wizard (or an existing
  config) plus the governed repo files (`AGENTS.md`, `CLAUDE.md`, `.gitignore`,
  `.pre-commit-config.yaml`, CI and deploy workflows, `README.md`,
  `deploy/tombstones.yml`) with no example app. `/start-app --setup` now runs
  it instead of `configure` alone.
- **`streamsnow deploy-setup --admin`**: the full, reviewable one-time admin
  bootstrap derived from `streamsnow.config.yaml`, in `USE ROLE` sections
  (SYSADMIN, USERADMIN, SECURITYADMIN, ACCOUNTADMIN, then the CI role): app
  database, schema and `XSMALL` warehouse, CI and viewer roles, a
  `TYPE = SERVICE` CI user with an `RSA_PUBLIC_KEY` placeholder, grants,
  governance reads (`USAGE` + `SELECT` per allowed schema, or
  `IMPORTED PRIVILEGES` for a shared database), and for the container runtime
  the PyPI external access integration on Snowflake's managed
  `snowflake.external_access.pypi_rule` plus compute pool `USAGE`.
  `CREATE COMPUTE POOL` is emitted only for a pool other than the
  pre-provisioned `SYSTEM_COMPUTE_POOL_CPU`.
- **`validate-app` `placeholders` check**: fails while any authored app file
  (query, page or `sql_review` manifest) still carries the scaffold's
  `YOUR_TABLE`, or while the starter page still shows its hard-coded sample
  metric and chart (marked `STREAMSNOW_STARTER_PLACEHOLDER`; the sample values
  themselves match too, so pages scaffolded by 0.7.0 are caught). CI deploys
  every app under `apps/`, so the placeholder app used to ship beside the real
  one. The sample values never read the query, so replacing `YOUR_TABLE`
  everywhere does not clear the page on its own.
- **`doctor` `snow-key-file`** (optional, warns): the default `snow`
  connection uses key-pair auth with no `private_key_file`, the signature of a
  key named `private_key_path` (see Fixed). Reads parameter names only, never
  values, and never edits the connection.
- **`sql-review check` `window` finding** (advisory, never gates under either
  coverage policy): a manifest with no `set_block` renders the implicit review
  window, the year ending `CURRENT_DATE`.
- **`preview logs` ends with a `cause:` line** for a known failure, including
  ones raised on the first page load after `start` already reported ready.
- **`examples/tpcds-demo/`**: a small store-sales extract from
  `SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL` into `STREAMSNOW_DEMO.TPCDS`, about two
  years with real seasonality, kept small to limit the scan. Its README covers
  the cost and how to point `/start-app` at any small table instead.
- **`SECURITY.md`**: report vulnerabilities through GitHub private
  vulnerability reporting; everything else through issues.
- **Generated `.gitignore` ignores `.internal/`** (local-only working notes).
  `.gitignore` is user-owned, so existing repos add the line by hand.
- **`doctor`**: `gh` (optional; `/ship-app` needs it) and, in a
  container-runtime repo, `container-python` (warns when no Python 3.11 is
  findable; fix: `uv python install 3.11`).
- **README "Who can use this"** and a getting-started block on what to ask a
  Snowflake admin for.

### Fixed

- **Every first CI deploy failed with `Connection default is not
  configured`.** Snowflake CLI 3.27.0 run with only `SNOWFLAKE_*` environment
  variables and no `config.toml` (a GitHub Actions runner) needs
  `--temporary-connection`, and the generated `deploy.yml` passed no connection
  flag. Every `snow sql`, `snow stage copy` and `snow git fetch` call in both
  deploy workflows now passes it; `verify-deploy` gains a
  `--temporary-connection` option the workflows pass (leave it off locally to
  use your default connection); and the passphrase secret reaches `snow` as
  `PRIVATE_KEY_PASSPHRASE`, the only name it reads for an encrypted key. The
  deploy workflows install `streamsnow>=0.7.1,<0.8`. Existing repos pick this
  up with `streamsnow update --apply`, which re-renders
  `.github/workflows/deploy.yml` (troubleshooting #19).
- **`streamsnow new` warns when repo files are missing**, naming them and the
  fix (`streamsnow init --no-starter-app`). Without them there were no hooks or
  CI, and nothing gitignored `.streamlit/secrets.toml`.
- **A fresh scaffold failed its own CI.** The generated `checks.yml` installed
  ruff unpinned while pre-commit pinned v0.15.9; newer ruff defaults flagged
  `I001`, `RUF100`, `C408` and `BLE001` in the templates. One `RUFF_VERSION`
  now renders into both files, and the templates are clean under 0.15.9 and
  current ruff defaults.
- **`doctor` passed a `snow` that crashes.** It now runs `snow --version`; on
  PATH but failing is a required `BROKEN` result with the
  `uv tool install snowflake-cli` hint. The `snow` probe timeout rose from
  5 s to 15 s, because a cold start reported "no connections".
- **Local preview install for warehouse apps.** `uv pip install -e apps/<slug>`
  fails without a `pyproject.toml`; docs, the scaffolded README, `init`'s
  `Next:` block and `streamsnow preview` now give the runtime-specific line.
- **`deploy-setup` suggested creating `SYSTEM_COMPUTE_POOL_CPU`**, which
  Snowflake pre-provisions. The default output keeps its statements and
  corrects the comment.
- **`init`'s `Next:` block** lists the plugin install first, matching docs
  Path B, and names the `YOUR_TABLE` step.
- **Warehouse apps pin Streamlit 1.52.2** (was 1.50.0), the newest version
  Snowflake supports in warehouse runtimes. Every supported version carries
  two Streamlit advisories fixed only in 1.53.1 and 1.54.0, so warehouse repos
  also get `osv_allowlist.json`: one dated entry per advisory ID, each with its
  reason, expiring 2026-12-31 for a re-check. `streamsnow update` creates the
  file in warehouse repos scaffolded before it existed.
- **Review SQL and default dashboards were empty on historical data.** With no
  `set_block`, a manifest's review window was the year ending `CURRENT_DATE`,
  so every per-visual `.review.sql` over data that ends in the past (TPC-DS
  ends in 2003) returned zero rows while `check` reported clean. The skills now
  require a window anchored to the source's `MAX(date)`
  (`review-app/sql-companions.md` step 3, `start-app/pages.md` 8.2), the page
  conventions say a page's default date range comes from the data's max date,
  not today, `check` names the implicit default, and the starter manifest
  shows the anchored form. An explicit `set_block`, including an explicit
  `CURRENT_DATE`, renders exactly as before.
- **Local preview crashed with key-pair connections**: `TypeError: Expected
  bytes, RSAPrivateKey, or EllipticCurvePrivateKey, got <class 'NoneType'>`.
  `st.connection("snowflake")` opens the default connection through
  snowflake-connector-python, which reads `private_key_file` but silently drops
  `private_key_path`, a legacy alias only the `snow` CLI understands, so `snow
  sql` worked on the same connection. The fix is to rename the key to
  `private_key_file` (passphrase: `private_key_file_pwd`), which both tools
  read; `doctor` and `preview logs` now say so (troubleshooting #17).
- **The wizard's default compute pool is `SYSTEM_COMPUTE_POOL_CPU`** (was
  `STREAMLIT_POOL`), so `deploy-setup --admin` for a default container config
  emits no `CREATE COMPUTE POOL`. A pool already named in a config is kept.
- **`connection_name` defaults to your default `snow` connection** when one
  exists and its `account` matches the account you answer (case-insensitive;
  was the folder slug), falling back to the slug when `snow` is missing or
  broken, or the default connection is for another account or names none (a
  one-line note says so, without printing either account). Users with a
  working connection from a prior tutorial no longer fail `doctor`'s
  connection check or get told to add a second `--default` connection; `init`
  and `configure` only print `snow connection add` when no connection by that
  name exists.
- **The starter trio from `streamsnow new`** (`queries/example_metric.sql`, its
  `sql_review` manifest, `pages/overview.py` with sample numbers) is replaced
  explicitly by `/start-app`'s build phase, and `new` says they are
  placeholders.
- **A fresh `init --no-starter-app` repo failed its own `checks.yml`**: with
  no `apps/` directory yet, `ruff check apps/` errored and `check tombstones`
  refused to run. Lint now runs only when `apps/` exists, and tombstones is
  skipped only while `apps/` is absent both locally and on `origin/main`.
- **`uv pip install -e apps/<slug>` failed on every fresh container app**
  (setuptools found `pages/`, `queries/` and `sql_review/` and refused to
  guess a package). The generated `pyproject.toml` declares `packages = []`;
  existing apps add the two lines from troubleshooting #18.

### Changed

- **Privacy gate.** `check_export_clean` holds generic checks only (personal
  paths, private keys, tokens, email addresses outside reserved example
  domains) and reads organization-specific terms from a gitignored
  `.streamsnow/export-denylist.txt`. A committed deny list shipped the names it
  guarded.
- **README positioning**: for internal analytics StreamSnow can replace a BI
  tool; external or customer-facing analytics needs additional customization.
- **Default Snowflake object names are StreamSnow-branded.** The wizard,
  `streamsnow.config.example.yaml` and the loader now default to
  `STREAMSNOW_APPS` (app and stage database), `DASHBOARDS` (schema),
  `STREAMSNOW_WH`, `STREAMSNOW_DEPLOY_ROLE` (whose CI user is
  `STREAMSNOW_DEPLOY_USER`), `STREAMSNOW_VIEWER_ROLE` and
  `STREAMSNOW_CODE_STAGE`, so `LIKE 'STREAMSNOW%'` finds every database,
  warehouse, role, user and stage `deploy-setup --admin` creates.
  `PYPI_ACCESS_INTEGRATION` is unchanged. Existing repos keep whatever their
  `streamsnow.config.yaml` says: a name set there wins over the default, and
  re-running `configure` keeps it. The one default an existing config can
  inherit is `snowflake.objects.stage_name`, which is optional and which the
  wizard never wrote: a stage-copy repo without it deploys through
  `STREAMSNOW_CODE_STAGE` from its next deploy (the deploy's `CREATE STAGE IF
  NOT EXISTS` creates it, using the CI role's `CREATE STAGE` grant). To keep
  the stage you have, add `stage_name:` with its name under
  `snowflake.objects`.

## [0.7.0] - 2026-09-11

The onboarding release. Everything here came from three sources read together:
a walk through the install path as a first-time user, the production fleet's
`.streamsnow/MIGRATION.md` (its honest reasons for pausing adoption at 0.6),
and a fresh read of the official Streamlit-in-Snowflake docs, which Snowflake
reorganized in early 2026. An adversarial Codex review of the plan removed a
comment-marker artifact waiver and a phase-suffix parser that would each have
weakened a gate; both landed as typed config instead.

### Added

- **Mission, vision and eight principles** at the top of the README (and in
  CONTRIBUTING as the review bar). Two principles are new and come from the
  fleet's pause: *faithful to a real fleet* and *leaving should be cheap*.
- **`sql_review: {coverage: warn | fail}`** in `streamsnow.config.yaml`
  (default `warn`). Every `sql-review check` finding now carries a `kind`
  (`coverage | fragment | collision | orphan | bind | provenance | readonly`);
  correctness kinds always fail, coverage follows the policy — in `validate-app`,
  pre-commit and CI alike. This replaces 0.6's "warn now, FAIL in 0.7" promise
  with a per-repo switch, and it is the fleet's own design note for the tool
  (it had been filtering coverage by matching finding *text*).
- **`deploy.artifact_exclude`**: typed exclusions for non-code files a deploy
  pipeline ships by another step (`.streamlit/config.toml` in the generated
  workflow and in the fleet). Exact relative paths only; `streamlit_app.py`,
  `pages/`, `queries/`, `*.py` and `*.sql` are rejected at config load, so the
  artifacts gate can never become an opt-out for code.
- **`**Phase notes:**`** line in §11: progress narrative lives there, and
  `**Current phase:**` stays the exact value `/start-app` resumes on.
- **Doctor checks:** `pre-commit` (optional without a config, required once one
  exists — the generated hooks are `language: system`) and `snow-connection`
  (does the configured `snow` connection exist; never runs `connection test`).
  The config result now carries `runtime` and `connection_name`.
- **`tests/fixtures/fleet/`**: an anonymized three-app repo mirroring the shapes
  that failed `validate-app` on every real app in MIGRATION.md (pipeline-shipped
  config.toml, narrative phase notes, a waived narrow session fallback,
  brace-less `-- Tokens:` headers, a package-qualified glossary helper, a nested
  page package). All three pass, and a second test keeps the shapes present.
- **`docs/snowflake-docs.md`**: every official Snowflake / Streamlit docs link
  the toolkit relies on, as a compatibility matrix with scope, retrieved date
  and StreamSnow's position where it deliberately differs. Offline registry
  test (`tests/test_docs_links.py`) plus an opt-in online sweep
  (`scripts/check_docs_links.py --online`, now in RELEASING.md).
- **`docs/troubleshooting.md`**: sixteen numbered symptom / cause / fix entries.
- **Skill/CLI parity test**: every `streamsnow <verb>` a skill or doc cites must
  exist in the CLI (argparse passthrough groups included).
- **SessionStart hook test** and a `skills/_shared/page-conventions.md` recipe
  (the glossary module and four-block page contract the fleet converged on).

### Changed

- **Onboarding is two lanes.** README and getting-started open with *With Claude
  Code* (three commands, `/start-app --setup` drives everything) and *CLI only*.
  `--setup` now installs the `streamsnow` CLI itself when missing and requires
  it before continuing; the `uvx` fallback is gone because every later skill
  calls bare `streamsnow`.
- **One connection store.** `snow connection add … --default` is the documented
  path; `st.connection("snowflake")` reads it locally, and the per-app
  `secrets.toml` is an optional override. `configure`, `init` and the preview
  classifier say so.
- **`streamsnow preview`** launches `<repo>/.venv/bin/streamlit` when it exists
  (the documented `uv venv && uv pip install -e apps/<slug>` never activates
  the venv), then PATH.
- **`/start-app` runs preview, validate and the review procedure itself** at
  the build/check phases instead of naming them for the user to type; it still
  stops at the three human checkpoints. Standalone verbs unchanged.
- **SessionStart banner** prints the plugin version, nudges `/start-app --setup`
  in a repo that has Streamlit apps but no config (it used to stay silent), and
  says when the CLI is missing from PATH.
- **`init`'s Next block** lists both plugin commands, the pre-commit install,
  `validate-app`, and the README Apps-table reminder; `new` prints the reminder.
- `snowflake-cli` everywhere (`snowflake-cli-labs` dropped from the doctor hint
  and both deploy workflow templates); generated CI pins `streamsnow>=0.7,<0.8`.
- Pre-commit hook `description:` blocks name the incident behind each check.
- Docs facts refreshed against the reorganized Snowflake docs: container runtime
  GA (2026-03-09), `get_active_session()` is warehouse-only and not thread-safe
  on containers, message limits differ by runtime, compute-pool packing, MFA
  rollout phase 3, `python=3.11` in the official `environment.yml` example vs
  our incident-backed gate, artifact repositories vs EAI.

### Known gaps

- PyPI access via a **Snowflake artifact repository** (now Snowflake's preferred
  path) is not emitted; the Snowflake CLI's `snowflake.yml` schema has no field
  for it yet. The generated EAI keeps working; do not attach both.
- `environment.yml` `python=3.11` (single `=`, as the official example shows)
  is still rejected. Re-test against a live warehouse app before relaxing.
- `check dependency-vulns` scans per-app manifests, not `uv.lock`.
- Deploy every app on every merge; a changed-apps-only option is planned.
- No `--vendor` mode yet; the exit path is documented as it is in
  `docs/distribution.md`.
- The generated `checks.yml` runs the governance gate, OSV, sql-review and
  tombstones but not `detect-secrets` or `ruff format` — those run in
  pre-commit only. `schema_allow` stays a convention; only `schema_deny` is
  enforced (documented in the generated AGENTS.md).
- The starter page renders mock numbers until `YOUR_TABLE` is repointed; a
  fresh scaffold therefore "works" without ever reaching Snowflake. Called
  out in the generated README.
- The deploy workflow exposes the Snowflake secrets to every step and pins
  actions by tag; scoping secrets per step and SHA-pinning are planned.

### Planned (next major)

- Fold `/preview-app` and `/validate-app` into `/start-app` modes and
  `/audit-lineage` into `/review-app --lineage`; drop the 8 pre-0.3 alias stubs
  (16 → 5 in the inventory, ~500 always-on tokens saved).

## [0.6.3] - 2026-09-05

Hotfix. Found by reviewing the 0.6.2 round-8 commit after it shipped — it had
never been reviewed before publishing.

### Fixed

- **`x$$y` was refused as an unterminated dollar-quote.** Snowflake permits `$`
  inside unquoted identifiers, so `x$$y` is a legal column name; 0.6.2's
  fail-closed guard treated every `$$` as a constant opener and refused the
  whole file. A `$$` now opens a constant only when it does not continue an
  identifier. The closing `$$` is unchanged, since a body may end in an
  identifier character (`$$abc$$`).
- **`COMMENT IF EXISTS ON …` and `COMMENT ON TAG|SHARE|MASKING POLICY|…`
  slipped past the SET-expression scan.** The command pattern omitted the
  documented optional `IF EXISTS` and most object types. Not executable after a
  `)` in Snowflake, but it falsified the stated invariant. Pattern widened.
- **`set_vars.name` accepted any non-blank string**, so `"bad name"` passed
  validation and rendered `SET bad name = 1;`. It must now be a session-variable
  identifier. A used entry missing `default` also raised `KeyError` in the
  renderer instead of being skipped.
- **`//` line comments were not masked** — Snowflake accepts them alongside
  `--`. An apostrophe inside one opened a phantom string literal that ran to the
  next `'` and hid real SQL from every guard, and because that literal
  *terminated*, the fail-closed path could not catch it. The sixth masking
  bypass of the same class. Found by a second review of the shipped commit,
  before this hotfix was tagged.
- **`CALL start()` had stopped being a command.** Excluding clause keywords
  after a verb (to stop refusing bare aliases) also excluded procedures named
  after them. `CALL` now also matches any identifier immediately followed by
  `(`, which a bare alias never is.
- Widened `COPY FILES INTO`, `UNDROP ICEBERG|DYNAMIC|EXTERNAL|EVENT TABLE`,
  `TRUNCATE IF EXISTS`; `NATURAL`/`ASOF` joins after a bare alias no longer
  refused; a name declared in both `set_block` and `set_vars` is rejected
  (it rendered two `SET` lines and the second silently won) — including against
  the implicit default `set_block` and case-insensitively, since session-variable
  names are, and the renderer itself now never emits a second `SET` for one
  name; tripwire messages
  now name only the verb; `_var_used` and `_BIND_RE` changes pinned by tests.

## [0.6.2] - 2026-09-03

Everything here came out of adopting 0.6.1 on a real 5-app repo with 231
queries and 28 existing audit manifests — the failures a greenfield scaffold
never surfaces.

### Fixed

- **Read-only guard bypass via double-quoted identifiers** (security). Snowflake
  treats `"…"` as a delimited identifier rather than a string, so the masker
  left it alone — and `WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t`
  passed `_verify_read_only` clean, because the `)` inside the identifier ended
  the CTE scan early and the trailing `SELECT` read as the terminal verb while
  Snowflake would execute the `DELETE`. A second review round found two more quoting
  forms with the same hole: **dollar-quoted constants** (`$$ ) SELECT y $$`),
  which were not recognised as a quoting form at all, and **backslash-escaped
  quotes** (`'\')`) — Snowflake accepts both `''` and `\'`, but only the
  doubling form was handled. All four quoting forms are now masked for
  structural analysis, and each is pinned in both directions.

  Masking identifiers also introduced a false *positive*: a legal delimited CTE
  name (`WITH "cte name" AS (SELECT 1) SELECT …`) made the CTE walker bail and
  the file be refused. Refusing to generate a legitimate audit file is a defect
  too, so the walker now accepts a quoted CTE name.

  It briefly introduced a **regression**, too: an odd `"` inside a `$$…$$`
  constant (`SELECT $$5" pipe$$ AS a; DELETE FROM t;`) masked to end-of-text and
  hid every following statement, turning a write 0.6.1 *caught* into one that
  passed. Dollar quotes are now consumed before `"` is treated as a delimiter.

  Four bypasses in one hand-rolled masker is a pattern, so there is now a
  second, independent layer: a write verb in command position is refused with no
  parsing of its own. Two anchors, deliberately different — at the START of a
  statement ANY of these verbs is a command, while right after a `)` only
  RESERVED words are checked, because that is where a bare column alias lives
  (`SELECT MAX(d) comment FROM t` is legal Snowflake, and `comment` is a real
  `INFORMATION_SCHEMA` column). The non-reserved verbs are still covered there
  in their two-token command form (`MERGE INTO`, `TRUNCATE [TABLE]`,
  `COMMENT ON <object-type>`, `COPY INTO`, `EXECUTE IMMEDIATE|TASK`,
  `UNDROP TABLE|SCHEMA|DATABASE`, `REMOVE|RM @`, `PUT file://`, and
  `CALL`/`UNSET` with an argument). Matching the verb plus "any identifier" is
  NOT sufficient: a bare alias is followed by a CLAUSE keyword (`FROM`,
  `WHERE`, `ON`, `,`) which is itself identifier-shaped, so clause keywords are
  excluded explicitly. `COMMENT ON` must name an object type, because
  `JOIN (SELECT …) comment ON a.id = …` is legal read-only SQL. There is no `;`
  anchor: statements are split on `;` before this runs. The
  allowlist depends on locating statement boundaries correctly and that has
  been defeated four times; the next parser gap should not also be a pass.

  Position, not bare tokens: most of these verbs are **not** Snowflake reserved
  words, so `SELECT 1 AS CALL`, `AS COPY`, `AS PUT`, `AS REMOVE`, `AS UNLOAD`
  and `AS EXECUTE` are all legal read-only SQL that a blanket token match
  rejected. Refusing to generate a legitimate audit file is its own defect.
  `COMMENT` and `UNDROP` are covered too. Zero false positives across the 30
  real audit files of the adopting repo — a floor, not a proof: review later
  found two more false-positive classes (a JOIN alias before `ON`, and a
  `set_block` expression continuing past a subquery), both now fixed and
  pinned.
- **Unsubstituted binds could ship in a rendered file.** A manifest declaring
  only `:1`/`:2` for a query that also uses `:3` emitted seven live
  `AND col <= :3` predicates. Every existing gate passed it: the allowlist
  checks statement roots, provenance checks input hashes, coverage checks
  claims — none asks whether the output *runs*. `generate` now refuses to write
  a file with a surviving `:N`, and `check` additionally audits the committed
  bytes (still import-free), so a hand-edited or pre-guard file is caught
  without regenerating. `Params:` banner comments stay exempt by construction.
- **SET blocks that promised a review window they did not control.** Three
  files declared `start_date`/`end_date`, referenced them zero times (their
  queries self-anchor on `CURRENT_DATE`/`DATE_TRUNC`), and carried a header
  telling the reviewer to edit those lines to change the window. The reviewer
  edits, reruns, gets byte-identical numbers, and signs off believing the
  window applied — a confidently wrong verification, which is worse than no SET
  block. The block is now pruned to the variables the body actually references,
  omitted entirely when none survive, and the header describes what was really
  emitted.

  Two follow-on fixes to that pruning: variable matching is **case-insensitive**
  because Snowflake identifiers are (`$START_DATE` is `$start_date`, and
  matching case-sensitively would prune a SET line that IS used, leaving a
  dangling reference — a worse failure than the unused line the pruning
  removes); and it reads masked text, so a `$name` inside a string literal or a
  quoted identifier no longer counts as a reference.

- **A referenced-but-undeclared session variable could ship.** SET-block pruning
  removed lines for declared variables a body never referenced; nothing checked
  the converse. A manifest whose `param_bindings` named a variable absent from
  `set_block` rendered `WHERE d BETWEEN $window_start AND $window_end` with NO
  SET block, under a header stating no section references a session variable,
  and both `generate` and `check` reported success. Pasted into Snowsight that
  dies on the first block. `generate` and `check` now refuse a `$var` with no
  `SET` line - the symmetric half of the bind check.
- **A `SET`-rooted statement escaped both read-only layers.** The tripwire used
  `search` (first match only) and then skipped the whole statement on the `SET`
  exemption, while the allowlist's `SET` form is prefix-only - so everything
  after the `=` was examined by neither, and `SET x = (SELECT 1) DELETE FROM t`
  passed clean. Not exploitable (Snowflake syntax-errors at the `)`), but it
  falsified the stated invariant. Now `finditer`, the exemption excuses only the
  match at offset 0, and the whole expression is scanned for the same reserved
  verbs and two-token commands the after-paren anchor looks for. To be precise
  about what that does and does not close: it is a denylist over those forms,
  so an unlisted command shape (`USE ROLE`, `GET @s`, `COMMIT`) still parses as
  a valid SET expression. None of those is executable after a `)` in Snowflake,
  and the root allowlist refuses them as statement roots — but the mechanism is
  a denylist, not a proof that "verbs nobody listed" are closed.
- **Path-dependent provenance, and two gates that silently passed.** Three tools
  filtered on the ABSOLUTE path's components, so a checkout under any dotted
  directory - a git worktree at `.claude/worktrees/<name>/`, which this
  project's own guidance recommends - made every file look hidden. Consequences:
  `sql-review` hashed zero app modules, so provenance differed between a
  worktree and a clean clone and a contributor saw false `DRIFT`; and
  `check app-security` and `check schema-refs` scanned NOTHING and reported OK.
  A gate that passes because it examined zero files is worse than no gate. All
  three now filter relative to the scan root, or by directory name where no root
  exists. Each argument carries its own root, so a checkout living under a path
  segment literally named `venv` or `node_modules` is scanned in full rather
  than skipped wholesale — while a `.review/` artifact handed over by explicit
  path is still skipped. Both fixes are mutation-covered: reverting either now
  fails tests, where before it left the suite green.
- **A `SET` statement's expression was unchecked past its prefix.** The
  allowlist's `SET` form was prefix-anchored, so `SET x = (SELECT 1) DELETE
  FROM t` matched it and every token after the `=` was examined by neither
  layer. Chasing that with more tripwire verbs fixed one verb at a time; a
  `SET` statement must now END where its expression ends, which closes the
  class including verbs nobody listed (`CALL`, `UNLOAD`, `UNSET`). An empty
  `set_block` value (`SET x = ;`) is rejected at validation, since it was
  invalid SQL that still read as a definition to the session-variable check.
- **Snowflake's multi-variable `SET (a, b) = (…)` was falsely refused** — the
  session-variable check recorded neither name and then reported both as
  undefined.
- **`COMMENT ON` matched a JOIN alias.** `JOIN (SELECT …) comment ON a.id =
  comment.id` is legal read-only SQL; it now requires an object type after
  `ON`, which a JOIN never has.
- **Duplicate `fragments` declarations across manifests** were silently
  accepted with the first reason winning; now reported.
- **A symlinked app module made provenance environment-dependent** again by
  hashing whatever the target held in that checkout; the link text is hashed
  instead.

### Added

- **`fragments` manifest field** — declares a `queries/*.sql` that is a shared
  CTE inlined via a token and therefore not independently runnable, exempting
  it from coverage. Without this the gate is unsatisfiable for any repo that
  factors CTEs into their own files. Exemption is explicit and never inferred
  from a filename, so it cannot be used to silence the gate by renaming; a
  declaration whose file no longer exists is itself a finding, and the `reason`
  renders into the README index.

  Because a fragment declaration *suppresses* a gate, its shape is validated
  strictly rather than best-effort: a bare list of strings (the form that looks
  like it works) is an error instead of a silent no-op, `file` must name exactly
  one file in `queries/` with no path separators or traversal (`../../x.sql`
  would otherwise reduce to a stem and exempt a different file), `reason` is
  mandatory, duplicates are rejected, and a query that is both page-claimed and
  declared a fragment is a contradiction rather than a silent last-wins. That
  last check works both WITHIN a manifest (rejected at validation) and ACROSS
  manifests (coverage resolves toward the stricter reading, ignores the
  exemption, and reports it) - the cross-manifest half was missing in review and
  produced two contradictory README rows for the same query, one of them reading
  `Verified: n/a` for a query that feeds a page.
- **`set_block_note` manifest field** — renders the rationale for the defaults
  (which source the bounds derive from, why that source and not the calendar,
  mechanics that bite when editing them) inline above the SET lines, where the
  auditor actually reads it.

## [0.6.1] - 2026-09-01

### Added

- **sql-review metrics mode** (`"mode": "metrics"` manifests): one AUTHORED
  runnable block per dashboard visual, in on-screen order, from files under
  `sql_review/_metrics/*.sql` (or a `queries/*.sql` when a visual is 1:1 with
  an app query — that reference also claims the query for coverage). A
  dashboard-map index heads the single rendered file. Sources are allowlisted
  to the two roots (traversal-safe), digest-pinned like every other input,
  and never imported — metrics blocks are static by nature. For dashboards
  whose visuals aggregate differently than any single app query.
- **Repo overlays** — project-level augmentation of the plugin's skills
  without forking them: every skill now reads `.streamsnow/overlays/<skill>.md`
  (committed, repo-owned) first, applying repo-specific additions/overrides;
  `overlays/all.md` applies to every skill. Overlay prose cannot disable the
  coded safety gates (hooks, validate, the sql-review read-only guard, CI).
  Contract: `skills/_shared/overlays.md`; a plugin-surface test pins the
  overlay point in every skill.

### Changed

- The generated `.gitignore` now excludes only `.streamsnow/preview/` (was all
  of `.streamsnow/`) so committed overlays fit under `.streamsnow/overlays/`.
  **Existing repos:** `.gitignore` is user-owned and `streamsnow update`
  never rewrites it — hand-edit the `.streamsnow/` line to `.streamsnow/preview/`
  before adding overlays, or git will ignore them silently.
- The migrate engine's Anaconda repodata cache moved into a private
  per-user directory (created 0o700, ownership-verified; unique-temp atomic
  writes). A predictable shared temp file could collide across users and be
  pre-seeded; any doubt about the directory now means fetch-fresh, never a
  trusted read.

## [0.6.0] - 2026-09-01

The enforcement release: the review-escalation loop becomes executable (one
gate, one Stop hook, loop primitives instead of prose), every app grows a
human-runnable SQL audit trail, and the generated pipeline gains its missing
delete path — detection automated, destruction only by committed consent.

> **Upgrading the plugin — reinstall required:** this release adds a `Stop`
> hook to `hooks/hooks.json`, and hook additions do **not** reach installed
> copies via autoUpdate (Claude Code pins the install path — see claude-code
> issue #52218, same dance as v0.4). Reinstall: `/plugin uninstall streamsnow`
> then `/plugin install streamsnow@streamsnow`, and relaunch.

> **Upgrading a consumer repo:** run `streamsnow update --apply` to receive the
> new pre-commit hooks (`page-imports`, `path-leaks`, `sql-review`,
> `dependency-vulns --best-effort`), the regenerated CI gates, and the deploy workflows'
> tombstone-reconcile step. The `validate-app` gate tightens — a
> previously-passing app can newly fail `page-imports`, `path-leaks`, or
> `requirements` (one-line fixes each; see **Changed** below). The new
> `sql-review` section only **warns** in 0.6, so missing audit trails don't
> block this upgrade. `deploy/tombstones.yml` is scaffolded on `init` for new
> repos only — `streamsnow update` never creates or rewrites it (it's a
> user-appended registry). In an existing repo, create the file by hand at the
> first rename or removal: a missing registry reads as empty, so
> `check tombstones` fails the PR until the file and its entry exist.

### Added

**Review gate + Stop hook**

- **`streamsnow review-gate`** (`classify` | `baseline` | `stamp` |
  `stop-hook`) — the single decision function for "does this change need
  review, and how deep?". In the source monorepo each caller hand-rolled its
  own substantive-vs-trivial bash and they drifted until the full review loop
  had no executable caller at all; now `/ship-app`'s preflight,
  `/feedback-app`'s follow-up, and the Stop hook all call the same `classify`.
  Coverage is **per-change, not per-app**: review artifacts record a
  `Reviewed-baseline:` digest plus per-file coverage keys computed from the
  AST shape (comments/docstrings stripped), so a comment reword stays
  reviewed while a logic change reopens exactly the files it touched.
- **Warn-only `Stop` hook** (`hooks/review_gate_stop.py`) — when a turn ends
  with a substantive app change no review artifact covers, it emits a one-line
  `systemMessage` pointing at `/review-app <slug> --auto`. Never blocks a
  turn; fail-open (any internal error exits 0); repo-gated on
  `streamsnow.config.yaml`; dedupes per (repo, slug, baseline) so the same
  unreviewed state nudges once. The payload is `systemMessage`-only on
  purpose: emitting `additionalContext` from a Stop hook was measured
  (2026-08-04) to start a fresh assistant turn with no user input — an
  unrequested turn per change. Off-switches: `REVIEW_GATE_OFF=1`, an
  `apps/<slug>/.review/SKIP` marker, or `review_gate: {enabled: false}` in
  `streamsnow.config.yaml` (block added to the example config). The hook
  executes the gate **by path** from the plugin root, so it works on
  plugin-only installs with no `streamsnow` pip package.
- **`streamsnow review-loop`** (`parse-findings` | `dedup-findings` |
  `write-resolutions` | `exit-condition` | `merge-findings`) — the
  deterministic primitives `/review-app --auto` runs each cycle, extracted so
  the loop doesn't re-derive its dedup from prose (which re-reports findings
  it already resolved and never converges). Dedup is against everything
  previously *resolved* — including judge-rejected findings — within a 7-day
  artifact window; `exit-condition` names its reason (`max-iterations`,
  `plateau`, `walk-degraded`, `walk-reentry`, `clean`, `continue`) so a
  transcript shows *why* the loop stopped.

**SQL-review audit trail**

- **`streamsnow sql-review`** (`discover` | `generate` | `check` | `index`) —
  every query under `apps/<slug>/queries/` (the convention UI-feeding SQL
  lands in; SQL inlined in Python is outside its reach) gets a fully-rendered,
  paste-runnable `.review.sql` under `apps/<slug>/sql_review/`, driven by
  per-feature JSON
  manifests in `sql_review/manifests/` (co-located with the app so a rename
  moves the audit trail with it). Rationale: apps store `{TOKEN}` + `:N`
  templates a reviewer can't run, and a dashboard whose numbers nobody can
  independently re-run is a dashboard nobody can sign off.
- **Import-free `check`** — each generated file carries a provenance line
  (`-- Provenance: schema=1 inputs=<sha256/16> output=<sha256/16>`) digesting
  the manifest, the referenced query templates, and (manifest strategy) the
  app modules the token dispatchers call into. `check` recomputes both hashes
  statically — it **never imports consumer app code** (a shared hook importing
  arbitrary modules would execute code on every commit) — so an edited
  template, manifest, module, or hand-edited rendered file all read as DRIFT,
  and every `queries/*.sql` must be claimed by some manifest (uncovered is a
  named failure, never a silent skip). Only `generate` may import app code,
  and only under the opt-in `manifest` token strategy on a developer machine.
- **Read-only guard** — rendered output is verified against a statement-root
  **allowlist** (`SELECT` / `WITH`→`SELECT` / `SHOW` / `DESCRIBE` / `EXPLAIN`
  plus `SET <ident> =` session variables), an allowlist rather than a
  write-verb denylist because the failure mode of a denylist is the statement
  type nobody thought of. Structural analysis runs with string literals and
  comments masked. Scope honesty (documented in the tool): this catches
  accidents and drift, not a deliberate committer — repo review remains the
  trust boundary.
- `streamsnow init` / `streamsnow new` render a starter manifest for the
  example query and run `sql-review generate`, so the audit-trail pattern is
  live in the tree from commit 1. Wired into pre-commit
  (`streamsnow-sql-review`), generated CI, and the validate gate (warn-only
  this release — see **Changed**).

**New tools and verbs**

- **`page-imports` check** — blocks bare imports of modules that live in an app
  subdirectory (`from _header import ...` for `pages/_header.py`). Deployed,
  only the app root is on `sys.path`; `streamlit run` *additionally* puts the
  executing page's own directory there, so this class boots clean locally,
  survives a full UI walkthrough, and then `ModuleNotFoundError`s on every
  affected page in production. Also flags the quieter variant: a name present
  both in a page's own directory and at the app root (or in stdlib, or in a
  dependency) resolves to a *different file* in each environment. Local
  verification is structurally incapable of catching either — the check is the
  only thing that can. (From a real four-page outage that shipped with green
  CI, a live local boot, and a clean click-through of every page.)
- **`dependency-vulns` check** — queries OSV.dev for every **exact** pin in
  `pyproject.toml` / `environment.yml` (one batched POST, stdlib urllib);
  a new CVE against an existing pin fails the next run, so the gate ages with
  the ecosystem, not the repo. Range pins are reported "unscanned" but never
  fail — they have no single version to query. Findings can be
  expiry-dated-allowlisted in `osv_allowlist.json`; `--best-effort` (the
  pre-commit default) warns instead of failing when OSV is unreachable, while
  generated CI runs it fail-closed as the authority.
- **`path-leaks` check** — blocks personal absolute paths
  (`C:/Users/<name>/…`, `/Users/<name>/Development/…`, `/home/<name>/…`) in
  committed `.py`/`.md`: broken for every other developer, and a real
  username leaked into a repo that may become public. Placeholder forms like
  `<user>` never trip it; the GitHub Actions `runner` home is exempt.
- **`tombstones` check + `deploy/tombstones.yml`** — the generated pipeline
  only ever runs `CREATE OR REPLACE STREAMLIT`; it has **no delete path**, so
  a renamed/removed app directory abandons its deployed object, frozen at the
  last merge and flagged unhealthy by `verify-deploy` forever after. The check
  diffs declared identifiers against `--base-ref` (via `git merge-base`, read
  with `git ls-tree` so it works across branches) and blocks a PR that stops
  declaring an identifier without tombstoning it in the same PR. Identity is
  `streamsnow.deploy.streamlit_fqn` of the slug — the *same derivation the
  deploy uses*, deliberately not a second parse of `snowflake.yml`.
  `--drop-sql` emits `DROP STREAMLIT IF EXISTS …` for the deploy reconcile
  step, and **refuses** (exit 2) when a tombstone matches a currently-declared
  app — dropping it would kill the app the same deploy just created. An
  unresolvable base ref exits 2: "could not compare" must never pass as clean.
- **`requirements` check** — validates the §11 Build Progress block
  (`**Current phase:**` with a recognized lifecycle phase, an append-only
  `Sessions` log whose last line is ISO-timestamped and names `Next:` for
  non-terminal phases) — exactly what `/start-app` resumes from, so a mangled
  hand edit becomes a named finding instead of silent amnesia. Apps without a
  `REQUIREMENTS.md` are not findings (spec presence is a build-phase concern).
- **`branding-parity` check** — every app ships its own `branding.py` copy, so
  a branding change never propagates by itself. The scaffold template now
  carries a `_BRANDING_VERSION` stamp; the check flags apps whose stamp lags
  the newest across apps, and only *notes* (never fails) unstamped pre-0.6
  copies and lag behind the installed template.
- **`streamsnow migrate`** (`preflight` | `scan-hardfails` | `translate-deps`
  | `graft-plan` | `scan-imports` | `scan-conformance` | `scan-inline-sql`) —
  the deterministic detection engine behind `/migrate-app`: JSON out,
  AST-only (no exec/eval/import of the source tree, so a hostile source can't
  run code on the migrating machine), secrets detected by *presence* only
  (contents never read, so nothing can leak into output or a transcript).
- **`streamsnow nav <slug>`** — AST enumeration of an app's pages
  (`st.navigation` dict/list forms, bare `st.Page` assignments, legacy
  `pages/`-dir, single-page), JSONL or `--json-array`, with a
  partial-enumeration warning when navigation is built dynamically — for
  walkthrough tooling that needs the ordered page list without regex.
- **`streamsnow doctor` per-check JSON** — `doctor` is rebuilt on a module
  returning one `{name, ok, level, detail, hint}` dict per prerequisite
  (`--json` / `--format json`), so `/start-app` preflight and fix-then-recheck
  loops stop scraping console text. `level` is per-result: a missing config is
  *optional* (a machine can be healthy outside any repo) but a present,
  invalid config is *required* — malformed must never read as "not configured
  yet".
- **`check artifacts --fix`** — repairs the `snowflake.yml` `artifacts:` block
  from files on disk as a minimal edit (only the block's own lines rewritten),
  also shipping referenced image/data assets that would 404 deployed; refuses
  (with a finding) manifests it can't rewrite safely.
- **`check session-fallback --base-ref`** — git-aware baseline mode: flag only
  files whose violation count *grew* versus the base ref, so adopting repos
  gate new debt without first paying down legacy debt (`--all` restores the
  tree-wide scan).

**Enforcement templates**

- Generated CI (`checks.yml`) now runs the deterministic gates
  **fail-closed**: `streamsnow check dependency-vulns` (no `--best-effort` —
  CI is the authority), `streamsnow sql-review check`, and `streamsnow check
  tombstones --base-ref origin/main` (checkout gains `fetch-depth: 0` for the
  diff). Verified by template regression tests in `tests/test_init.py`.
- Both deploy workflows (stage-copy and git-repository) gain a **Reconcile
  tombstones** step: every registry identifier is dropped on every deploy via
  `check tombstones --drop-sql` (`IF EXISTS`, so re-runs are no-ops; a
  malformed registry exits 2 *before* any DROP; the live-app guard re-checks
  on the deploy side because a direct push to main never saw the PR check).
- `streamsnow init` scaffolds `deploy/tombstones.yml` as an empty,
  self-documenting registry, excluded from `streamsnow update` re-renders —
  it's user-appended consent, not a governance file to regenerate.
- The 8 skills are rebuilt on the new tools: `/ship-app`'s preflight and
  `/feedback-app`'s follow-up step call `review-gate classify` (asks, never
  blocks — shipping unreviewed stays available; validate + CI are the real
  publish gates),
  `/review-app --auto` runs on the `review-loop` primitives, `--sql` drives
  `sql-review`, and `/migrate-app` reasons over `streamsnow migrate` JSON.

### Changed
- **`validate-app` tightens** — four new sections:
  - `page-imports` (fix: package-qualify the import, `from pages._x import …`);
  - `path-leaks` (fix: replace the personal path with a placeholder or docs
    link);
  - `requirements` (fix: restore the §11 `**Current phase:**` line / a
    timestamped last session line with a `Next:` hint);
  - `sql-review` — **warn-only in 0.6, planned to become a FAIL in 0.7**, so
    adopters get one release to backfill audit trails (fix: `streamsnow
    sql-review discover <slug> --write`, edit the manifests, `generate`).
  `dependency-vulns` is deliberately NOT in the aggregate (it needs the
  network; the gate stays no-DB/no-network) — it runs as its own pre-commit
  hook and CI job instead.
- **`streamsnow preview` is now a lifecycle**, not a blocking foreground run:
  `start` (detached launch, log capture, `/_stcore/health` poll, classified
  launch failures — missing secrets, bad account locator, missing package,
  port collision — instead of a raw traceback), `status`, `stop`
  (idempotent, process-group SIGTERM→SIGKILL), `logs` (kept after stop for
  post-mortems). State lives per-repo in `.streamsnow/preview/` (gitignored by
  the scaffold; nothing global). Bare `streamsnow preview <slug>` remains a
  shorthand for `start`; the old `--dry-run` flag is gone.
- Generated CI and deploy workflows install a **pinned range**
  (`uv tool install 'streamsnow>=0.6,<0.7'`) instead of an unpinned latest, so
  a future 0.7 gate-tightening can't fail consumer CI unasked.
- `AGENTS.md` (generated) and the start-app page recipe now state the
  package-qualified import rule, so a scaffolded repo teaches it before an
  agent has a chance to imitate the wrong form from a sibling page; the
  repo-level template also documents the `sql_review/` audit-trail convention.
- The validate-app skill's "What it covers" list was three checks out of date
  (`artifacts`, `sql-tokens`, `session-fallback` were missing).

### Fixed
- **`streamsnow update` outside a configured repo tracebacked** with a raw
  `FileNotFoundError` instead of the "run `streamsnow init`" guidance every
  other path gets: `load_config` with an explicit `--config` path that doesn't
  exist now maps `OSError` to the friendly `ConfigError` (seen live;
  regression-tested in `tests/test_config.py`).

## [0.5.0] - 2026-07-20

Production-lessons release: the guardrails a real Streamlit-in-Snowflake fleet
accumulated — three new governance checks, post-deploy health verification, and
a written catalog of the failure modes behind them.

> **Upgrading a consumer repo:** run `streamsnow update --apply` to receive the
> new pre-commit hooks, the deploy workflow's verify step, and the AGENTS.md
> Production rules section. The `validate-app` gate tightens on upgrade —
> previously-passing apps can newly fail `artifacts` / `sql-tokens` /
> `session-fallback`; fixes are one-liners, see the validate-app skill's
> `fixing-checks.md`. No plugin hook changes, so no reinstall dance this time.

### Added
- **`artifacts` check** — cross-checks `snowflake.yml`'s `artifacts:` list
  against the files on disk. Local dev reads disk while a manifest-driven
  deploy reads the list, so an uncovered new file works locally and silently
  404s deployed; a stale entry breaks the deploy. (A recurring production
  incident — twice in one fleet, months apart.)
- **`sql-tokens` check** — flags `{TOKEN}` placeholders inside SQL comments.
  `render_sql` substitutes tokens with comment-unaware `str.replace`, so a
  documented token in a comment expands into live SQL and parse errors.
- **`session-fallback` check** — requires a broad `try/except Exception`
  around `get_active_session()` (it raises during local `streamlit run`;
  narrow `except ImportError` misses resolver-dependent failure types).
- **`streamsnow verify-deploy <slug> [--sha]`** — post-deploy health
  verification, because "deploy succeeded" is not "app serves": object exists,
  `live_version_location_uri` is set (NULL renders nothing in Snowsight),
  version-source URI contains the merge SHA (stage-copy), and container
  service logs show no crash-loop signature. Retries absorb container cold
  start; the log scan is strictly fail-open. Both generated deploy workflows
  gain a `Verify deploy health` step.
- **`docs/production-lessons.md`** + **`skills/_shared/production-gotchas.md`**
  — the full catalog and the condensed symptom→rule table: owner's-rights
  grants, passthrough-view pushdown, dynamic-table role/layering rules,
  out-of-band DDL audit trail, local SQL-cache restart, verify-runtime-before-
  diagnosing, `None`-in-`params=`, retire-by-moving. Pointers wired into the
  review-app, audit-lineage, preview-app, and validate-app skills.

### Changed
- Pre-commit template gains three hooks (`streamsnow-sql-tokens`,
  `streamsnow-session-fallback`, `streamsnow-artifacts`).
- AGENTS.md template gains a **Production rules** section and a correction:
  deployed apps execute with **owner's rights** (the ci_role's grants), not
  caller's rights as the viewer role.
- Template regression tests now pin the deploy workflows' concurrency
  serialization and the `.streamlit/config.toml` dotfile-copy loop (which
  `snow stage copy --recursive` would otherwise silently skip).

### Fixed
- **Scaffolded apps could crash-loop after a Snowflake base-image rollout.** The
  container app template pinned `streamlit==1.50.0`. The container runtime's base
  image launches Streamlit with CLI flags from its own bundled build (e.g.
  `--server.unsafeMetricsUserAttributes`, added in Streamlit 1.59.0); a pin below
  what the current image expects makes Streamlit reject the flag and crash-loop on
  startup (service READY, app never serves). Bumped the container template pin to
  `streamlit==1.59.2` and documented the base-image floor rule in both dependency
  templates. The warehouse template pin is left as-is with a note that the floor is
  container-only (Snowflake runs its own bundled Streamlit there; the conda pin is
  cosmetic).

## [0.4.0] - 2026-07-15

Safety + adoption release: the deploy-safety guard jobwright pioneered arrives in StreamSnow,
plus a load-blocking manifest fix and the trust/discoverability hardening the whole plugin
family shipped together.

> **Upgrading an existing install:** hook additions do not reach installed copies via
> autoUpdate (Claude Code pins the install path — see claude-code issue #52218). Reinstall:
> `/plugin uninstall streamsnow` then `/plugin install streamsnow@streamsnow`, and relaunch.

### Fixed
- **Plugin failed to load on Claude Code ≥ 2.1.** `plugin.json` declared
  `"hooks": "./hooks/hooks.json"`, but current Claude Code auto-loads that standard path, so
  the manifest key pointed at an already-loaded file and aborted the whole plugin with
  "Duplicate hooks file detected" — skills included. Exactly the bug jobwright fixed in its
  v0.1.1; the key is removed and a regression test now keeps it out
  (`tests/test_plugin_surface.py`).

### Added
- **Deploy-safety guard** (`hooks/deploy_safety.py`, PreToolUse) — ported from jobwright.
  Pauses for confirmation before destructive Streamlit/SQL commands: `snow streamlit
  deploy`/`drop`, `CREATE OR REPLACE / DROP / ALTER STREAMLIT`, stage `REMOVE`, and
  destructive SQL through any warehouse CLI, including SQL hidden in `-f` files and stdin
  redirects. Defends against shell-quote and full-path evasion; repo-gated on
  `streamsnow.config.yaml`; stdlib-only; fail-open (only ever *adds* a confirmation).
  The session banner now announces the guard — an invisible safety net reads as no safety net.
- **Frontmatter parity with jobwright**: every skill now declares `argument-hint` and
  `allowed-tools` (fewer permission prompts), and `/ship-app` + `/migrate-app` carry
  `disable-model-invocation: true` — shipping and migrating are human decisions.
- **System-evolution retro** at the end of `/ship-app` (ported from ticketwright's `/ship`
  Phase C): when something went wrong, fix the layer — config, skill, check, or deploy path —
  not just the instance.
- **Hooks-in-full README section**: every hook, what it does, the stdlib-only/no-network/
  fail-open guarantees, and how to disable — hook transparency is the trust bar for plugins
  that run PreToolUse guards.
- **Explicit hook timeouts** (SessionStart 5s, PreToolUse 10s) so a hung hook can never stall
  a session, and a `plugin-validate` CI job (`claude plugin validate . --strict`).

### Changed
- The 80-line SKILL.md cap is now measured on the **body** (after frontmatter) — frontmatter
  grew for parity and shouldn't force cutting instructions.

### Deferred (noted for a future release)
- Skill trigger evals (skill-creator description-tuning); submission to
  `claude-plugins-community`; multi-harness install docs.

## [0.3.0] - 2026-07-02

The UX release: **14 skills → 8**, one front door, ≤5-question setup, plain language on every
user-facing surface. Engine changes are minimal — the CLI checks, scaffolder, deploy generators,
hooks, and templates carry over (one exception: the `configure` wizard slimmed down); this is a
surface-area consolidation, applying the design system shipped in Ticketwright v2.0.

### Changed — the rename map (v0.2 → v0.3)
| v0.2 | v0.3 |
|---|---|
| `start-app` + `new-app` + `refine-requirements` + `add-page` + `onboard` | **`start-app`** (the front door — owns spec → scaffold → build → ship; `--spec` for the requirements phase incl. **backfill from existing source**, `--setup` for machine + repo setup, `adopt` for existing repos) |
| `review-app` + `apply-review` + `auto-review-app` + `sql-review` | **`review-app`** (`--fix` applies findings as atomic commits, `--auto` loops review→fix to convergence, `--sql` writes the audit companions) |
| `deep-dive-data` | **`audit-lineage`** |
| — | **`feedback-app`** (new — upstreamed from production use: classify user feedback into BUG / POLISH / UX / NEW-FEATURE / CROSS-CUTTING, apply as atomic per-item commits, follow-up review) |
| `preview-app`, `validate-app`, `ship-app`, `migrate-app` | unchanged names, refreshed surfaces |

All 8 retired names still work as deprecated alias stubs (`commands/`); they will be removed in
the next major release.

### Added
- **Adopt mode** (`skills/start-app/adopt.md`) — `/start-app adopt` on a repo that already has
  Streamlit apps maps onto the observed layout instead of scaffolding over it, classifies custom
  commands/skills as shadows / extends / unrelated against the plugin's skills, and writes a
  `MIGRATION.md` checklist. An existing `AGENTS.md` is never overwritten (renders to
  `AGENTS.streamsnow.md` for manual merge).
- **≤5-question `streamsnow configure`** — down from ~14 prompts. Asks only runtime, account
  locator, governed database, allowed schemas, and deploy source; everything else (project
  identity derived from the directory name, roles, warehouse, schema names, container objects,
  git-repository deploy fields) is written as an **inline-commented default** in
  `streamsnow.config.yaml` — the file is the editing surface, and re-running `configure` prefills
  from it so hand edits survive. Guarded by a new `tests/test_wizard.py` contract test.
- **Plugin-surface contract test** (`tests/test_plugin_surface.py`) — CI now asserts the 8-skill
  surface, the ≤80-line SKILL.md cap, the 8 alias stubs pointing at their replacements, no retired
  name referenced as live inside `skills/`, and every relative markdown link resolving.
- **Spec backfill** (`/start-app --spec <slug>` on an app with existing code) — reverse-engineers
  `REQUIREMENTS.md` from `st.Page` declarations, chart/KPI/filter calls, SQL header blocks, cache
  decorators, and `snowflake.yml`, marking anything uncertain `(inferred)` for §10 review.
  Upstreamed from proven production use.
- **`skills/_shared/runtime-decision.md`** — the container-vs-warehouse decision in one place,
  neutrally framed (both runtimes are legitimate; the repo default wins absent a concrete reason),
  with the detection rule, trade-off table, and the manifest/connection checklist. Skills now link
  to it instead of re-explaining the choice (previously restated in 5+ skills).
- `docs/migrating-a-consumer-repo.md` — map a repo's home-grown skills to the plugin surface
  (worked example: a 16-skill production repo), what stays local (branding parity, tracker
  integration, warehouse-specific rules), and the incremental adoption path.

### Changed (language & structure)
- **SKILL.md ≤80 lines, depth in reference files** — every skill's front page is now a scannable
  contract (modes, steps, boundaries), with detail split into per-skill reference files
  (`start-app/{spec,scaffold,pages,setup,adopt}.md`,
  `review-app/{dimensions,fixes,auto-loop,sql-companions}.md`, `audit-lineage/tracing.md`,
  `feedback-app/classification.md`, `validate-app/fixing-checks.md`).
- **Plain language on user surfaces** — skill descriptions lead with the trigger use-case;
  "deterministic PASS/FAIL ship gate" reads "the pass/fail check that must be clean before
  shipping"; report summaries print critical / should-fix / nice-to-have; "legacy" dropped from
  warehouse-runtime framing. Contributor-facing terms (bucket mechanics, check internals) stay in
  reference files.
- **Graceful degradation instead of hard failure** — missing config: review runs static-only and
  says which findings go unverified; missing Snowflake connection: lineage/companion rows are
  marked unverified with the exact enabler named; missing Playwright MCP: walkthroughs skip
  silently. Skills name the enabler instead of refusing.
- **§11 Build Progress simplified** — `Current phase` plus an append-only Sessions log whose last
  line names the next command. The per-page status table is gone (page state is visible in the
  tree and git); existing specs keep working, the table just stops being maintained.
- `hooks/session_start.sh` discovery line, README, and docs updated to the 8-skill surface.

### Carried from the previous Unreleased
- `docs/distribution.md` — how StreamSnow ships (PyPI CLI + Claude Code plugin)
  and the recorded decision **not** to add a separate `cp -r` copyable kit
  (`streamsnow init` is the config-driven "better `cp -r`").
- Thickened the plugin skills toward the source's depth — that content now lives in the v0.3
  reference files rather than monolithic SKILL.mds.

## [0.2.0] - 2026-06-30

### Fixed
- The scaffolded `branded_metric` now HTML-escapes its label/value/delta before
  rendering with `unsafe_allow_html=True`, so a database-derived value cannot
  inject markup into the viewer's page (hardening applied to the template and the
  example). Dependency-name matching is PEP 503-normalized, so a manifest that
  spells a package with underscores/dots (`snowflake_snowpark_python`) is no
  longer reported as missing.
- `validate-app` now validates the **contents** of the sibling dependency
  manifest, not just its presence: container apps must declare a
  `requires-python` that admits the container runtime's Python (PEP 440
  specifier semantics, so `>=3.10` is accepted and `<3.11` / `==3.10.*` are
  correctly rejected) plus `streamlit` + `snowflake-snowpark-python`; warehouse
  apps must declare those deps in `environment.yml` and must not pin `python`.
- `check caching` now flags two patterns it previously missed: a public loader
  that hands a **named query through a local variable**
  (`sql = load_sql("x"); conn.query(sql)`) and one that **delegates** a named
  query to a private fetch helper (including transitive helper chains). Only the
  SQL-bearing argument is inspected, so an unrelated string keyword (e.g.
  `query_tag="adhoc"`) no longer trips the generic-executor guard.

### Added
- Documentation guides: `docs/getting-started.md` (try the example with no
  Snowflake, then set up a governed repo), `docs/data-discovery.md` (find tables
  and wire governed queries), and `docs/deploying.md` (the end-to-end deploy
  story for both deploy sources). Linked from the README.
- Runnable example app at `examples/sample-dashboard/` — a StreamSnow-shaped
  Streamlit dashboard wired to deterministic sample data, so it renders with
  `streamlit run` and **no Snowflake connection**. Mirrors the `streamsnow init`
  structure (st.navigation entrypoint, branding, `@st.cache_data` loaders).
- `packaging` runtime dependency (PEP 440 version-specifier parsing in
  `validate-app`).

## [0.1.0] - 2026-06-27

Initial release.

### CLI (`streamsnow`)
- `configure` / `init` / `new` — set up the Snowflake environment and scaffold a
  governed monorepo + apps (container or warehouse runtime).
- `doctor` — machine + config prerequisite checks.
- `validate-app` — deterministic PASS/FAIL gate; `check schema-refs|security|caching|bind-predicates`.
- `preview` — run an app locally against live Snowflake.
- `deploy-setup` / `deploy-sql` / `stage-path` / `config-get` — deploy SQL + helpers
  for stage-copy and git-repository sources.
- `update` — re-render governance files from the current config (dry-run by default).

### Claude Code plugin
- 14 skills (onboard, refine-requirements, new-app, add-page, preview-app,
  validate-app, ship-app, start-app, review-app, deep-dive-data, apply-review,
  auto-review-app, sql-review, migrate-app) + 4 shared recipes.
- SessionStart hook, guarded to StreamSnow repos.

### Governance & safety
- Typed, validated `streamsnow.config.yaml` with an injection-safe rendering gate.
- Config-driven schema allow/deny, app-security, caching-TTL, and bind-predicate checks.
- Pre-publish privacy/export gate; generated repos ship pre-commit + CI guardrails.

### Packaging
- PyPI Trusted-Publishing release workflow; wheel-smoke + 3.11/3.12 CI matrix.
