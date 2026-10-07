# Setup: machine, repo and Snowflake account

Get a fresh machine and repo ready to build and preview apps, for the person creating the repo and
for every teammate who clones it later. `streamsnow doctor` is the source of truth: don't
re-derive prerequisites by hand (no `which python`, no version greps).

**You do the setup; the user approves.** The person running this may know nothing about
StreamSnow or whoever sent it to them, so trust comes from saying plainly what you are about to do
and why. Follow the narration rules in [SKILL.md](SKILL.md).
Installs need one batched approval: list every missing tool with one line of what it is and why
they need it (the table in §1 has the wording), let the user drop any, take one "yes", then
install them one at a time, re-running `streamsnow doctor --format json` after each and
reporting the result in one line. Every other change still gets its one-line what and why
before it runs. Example of one line in that list:

> Node.js: a small runtime StreamSnow uses to drive a browser that clicks through your app and
> screenshots each page, so you see problems before you ship (`brew install node`).

Plain words, no walls of text. If they drop an item, mark it skipped and move on.

Run everything yourself except the few steps that need the user's own identity or an admin.
Hand those over with a one-line reason:
- browser sign-ins (approving `snow connection test` or `gh auth login` in the browser): only
  they can sign in as themselves;
- `/reload-plugins`: a slash command only the user can type (needed only if the plugin's skills
  aren't showing in the session);
- the Snowflake admin SQL from `streamsnow deploy-setup --admin` (§2d): it needs admin rights,
  and you never run it;
- anything that asks for their computer password (`sudo` on Linux/WSL): your shell cannot answer
  that prompt, so give them the command; prefer installs that need no password (uv, nvm).

The CI key is handled only by `streamsnow ci-key create`, `streamsnow ci-key push` and
`streamsnow ci-key verify` (§2d, §2e).
The plugin's key guard blocks every other tool call that names `~/.streamsnow-ci`, so a blocked
call there is expected: never try another way to read a key or secret file.

## 0 · Windows: use WSL

If your environment reports a native Windows platform (`win32`; inside WSL it reports `linux`),
stop before installing anything. Say, in two lines: StreamSnow runs inside WSL (Windows Subsystem
for Linux), Microsoft's built-in Linux layer for Windows, because its local preview and safety
hooks only work on macOS and Linux today; it is a one-time setup and the user's apps work the same
there. Then walk them through it: open PowerShell as administrator, run `wsl --install`, restart
when asked, open the new Ubuntu app, install Claude Code inside it, and run `/onboard`
again from there. Doctor's `platform` row repeats this for anyone running the CLI directly.

## 0b · The CLI itself

Every step below, and every other skill, shells out to `streamsnow`. If `command -v streamsnow`
finds nothing, propose `uv tool install streamsnow` (after `uv` exists — install that first if the
doctor would flag it), run it on confirmation, and confirm `streamsnow --version` answers before
moving on. This is required, not optional: there is no `uvx` fallback, because the later skills
call the bare command. `streamsnow: command not found` after a successful install means the tool
bin dir is not on PATH yet — have the user re-open the shell.

Report which `streamsnow` answered and its version (`command -v streamsnow`, `streamsnow --version`),
from the same shell the later steps run in. When the user is testing a source checkout or a pinned
version that differs from the global install, call that executable by its path for every step.

## 1 · Machine prerequisites

Run `streamsnow doctor --format json` and read the per-check results — each check is one object:

```json
{"name": "uv", "ok": false, "level": "required", "detail": {...}, "hint": "install uv — ..."}
```

Report in one line ("5 checks passed, 2 need attention"). Propose a fix for every `ok: false`
check (start from the check's own `hint`; the table below gives per-OS commands) as one batched
approval, as described at the top. Then run the approved fixes one at a time, re-running
`streamsnow doctor --format json` after each and confirming that check now reads `ok: true`
before the next. `level` decides severity: a `required`
failure blocks the build phases (doctor exits 1); an `optional` one (`snow`, `streamlit`, `gh`,
`snow-connection`, `snow-key-file`, `container-python`) is offered, skippable. `snow` flips to `required` when it
is on PATH but `snow --version` fails (`BROKEN`): reinstall with `uv tool install snowflake-cli`.
A `snow --version` that does not answer within 45s is a `warn`, not `BROKEN`: a cold start can
take that long, so re-run doctor rather than reinstalling; if `snow connection list` is silent too,
`snow-connection` and `snow-key-file` say "not checked".
Five onboarding rows follow the machine checks. `repo-files`, `git-identity` and
`pre-commit-hook` are `required` once a config exists (skipped or a warning before that): a
governed repo needs its hooks, CI and `.gitignore` on disk, commits need a name and email, and
`pre-commit install` runs per clone, so without the hook a teammate's commits skip every check.
`git-identity` reports only whether each value is set; ask the user for their name and email,
and prefer repo-local scope on a machine with several accounts. `node` is an optional warning: the
Playwright CLI that walks your app runs through `npx`, and without it every UI walkthrough is skipped.
`ci-secrets` belongs to §2e. `gh` is optional here and required later by `/ship-app`. `container-python` warns in a
container-runtime repo with no Python 3.11 (`uv python install 3.11`). Two checks flip level by context: `config` is `optional`
when no `streamsnow.config.yaml` exists yet and `required` when one exists but fails validation
(a malformed config is never masked as "not configured"); `pre-commit` is `optional` on a bare
machine and `required` once a config exists, because the generated hooks are `language: system`
and the repo's first commit fails without the executable. If the user declines a fix, mark it
skipped and continue. Exit codes: 0 = all required checks pass, 1 = a required check failed,
2 = the doctor itself failed (report the error verbatim).

Skip `doctor` when the user has ruled out inspecting local connection settings: it reads
`snow connection list` for the `snow-connection` and `snow-key-file` checks. Say it was skipped and
report the prerequisites, connection readiness included, as unverified.

| What it is | Why they need it (say this) | macOS | Linux / WSL |
|---|---|---|---|
| Python 3.11+ (blocker) | the language StreamSnow and the apps are written in | `brew install python@3.11` | distro package, or `uv python install 3.11` |
| uv (blocker) | installs Python tools and keeps each app's packages separate | `brew install uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| git name and email (blocker once configured) | every saved change (commit) records who made it | `git config user.name "…"` and `git config user.email "…"` | same |
| Snowflake CLI `snow` (optional) | lets the apps on this computer sign in to Snowflake, through one saved connection | `uv tool install snowflake-cli` (a Homebrew `snow` can break on a newer system Python) | `uv tool install snowflake-cli` |
| pre-commit (blocker once configured) | runs StreamSnow's safety checks automatically before each commit | `uv tool install pre-commit` | same |
| pre-commit hook (blocker once configured) | connects those checks to this copy of the repo; each clone needs it once | `pre-commit install` | same |
| Node.js 20+ (optional, recommended) | runs the Playwright browser tool that clicks through the app and screenshots each page | `brew install node` | nvm (`nvm install --lts`); distro packages are often too old |
| GitHub CLI `gh` (optional; `/ship-app` needs it) | opens pull requests and checks the deploy settings on GitHub | `brew install gh`, then `gh auth login` (user signs in) | distro package, then `gh auth login` |

After the repo is configured, run `pre-commit install` yourself (doctor's `pre-commit-hook` row
confirms it). In git worktrees a repo-managed `core.hooksPath` can make it refuse; confirm with the
user before unsetting it, since it is sometimes intentional. If another tool's hook is already
there, `pre-commit install` keeps it as `pre-commit.legacy` and still runs it.

### 1b · Browser check (advisory, never blocks)

StreamSnow walks each page of a running app in a browser with the Playwright CLI, through `npx`,
at the pinned version in [_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md)
(`P` below means `npx -y @playwright/cli@<that version>`). Prepare it now rather than finding out
later when a review silently skips it. One line to the user: "Downloading the browser tool now,
so app walkthroughs work later."

- Doctor's `node` row not ok: Node.js 20+ is in the batched install (see the table), first.
- Download the CLI and its browser: `P --version`, then `P install-browser chrome-for-testing`
  (no sudo needed). Ignore the CLI's update and install banners; never install `@latest` or
  globally.
- Smoke test from a temporary directory, so no CLI files land in the repo:
  `(cd "$(mktemp -d)" && P -s=streamsnow-check open about:blank --browser=chromium --idle-timeout=60000)`,
  then `P -s=streamsnow-check close`. Report "browser tool works" in one line.
- On Linux or WSL, a launch that fails on missing system libraries needs one command the user runs
  (it asks for their password): `sudo npx -y @playwright/cli@<that version> install-browser --with-deps chrome-for-testing`.
- Each new `npx` command can ask the user for permission. To cut prompts, they can allow
  `Bash(npx -y @playwright/cli@*)` in their Claude Code settings; mention it once.
- Still failing: say the UI walkthrough will be skipped until it is fixed, point to
  `docs/troubleshooting.md` #20, and continue. Nothing else depends on it.

### 1c · The project's Python environment

Local preview runs the app with the repo's `.venv` (it prefers `.venv/bin/streamlit`, or
`.venv\Scripts\streamlit.exe` on Windows). If the repo root has no `.venv`, create it now:
`uv venv --python 3.11`. One line to the user: "Creating this repo's Python environment, where
each app's packages get installed." Do not install packages here: the exact versions depend on
the app's runtime, and `/build-app` installs them right after it scaffolds the app. If `.venv`
already exists, leave it alone.

## 2 · Repo configuration + governed repo files

**Never run only `streamsnow configure` here** (`configure` followed by `init`, as in 2c, is
fine): `configure` writes the config file and nothing else, and `streamsnow new` writes app files
only, so a repo set up that way has no hooks, no CI and no `.gitignore` (an app's
`.streamlit/secrets.toml` could then be committed). `streamsnow new` warns when those files are
missing; the fix is `streamsnow init --no-starter-app`, which runs the config wizard (or reuses an
existing `streamsnow.config.yaml`) and then writes the governed repo files: `AGENTS.md`,
`CLAUDE.md`, `.gitignore`, `.pre-commit-config.yaml`, `.github/workflows/`, `README.md` and
`deploy/tombstones.yml`. It writes no example app; `/build-app` scaffolds the real one with
`streamsnow new`. Which case applies (no config, apps without config, config with missing files)
is decided in [SKILL.md](SKILL.md) Stage 3.

When `streamsnow.config.yaml` already exists, run `streamsnow init --no-starter-app` as is: it
reuses the file and writes only the missing repo files. Skip the proposals below.

The wizard asks **at most 5 questions**: runtime, Snowflake account, the database apps query, the
allowed schemas, and the deploy source. Everything else (project name, roles, warehouse, schema
names, container objects) is written as a sensible default with an inline comment saying when to
change it; the file is the editing surface. Don't make the user answer those questions cold:
investigate, explain each setting, ask each one with your proposal as the recommended option, then
pass the confirmed answers as flags.

**The user decides; everything below is a proposal.** Before probing, say in one line what you
will read and from which connection, so the user can redirect it or decline. The user can:

- skip the investigation and answer the five questions themselves, or run
  `streamsnow init --no-starter-app` in their own terminal and answer the wizard there;
- choose which connection, MCP server or role you investigate with;
- override any proposed answer, including choosing `container` or `git-repository` when the
  evidence points elsewhere (say what that choice needs, then respect it);
- change any default the wizard does not ask about (2c, before `init` renders the repo files);
- stop at any point and carry on later.

Two limits are not the user's to waive, because they protect the user: you never run DDL or
grants (the admin runs `deploy-setup --admin` output), and you never handle a password, token or
key in chat (the user types those into their own terminal).

### 2a · Find the user's Snowflake access

**Inventory first.** Check every place this machine or repo can hold Snowflake access, reading
presence and names only, never values, and report one line ("found: 2 snow connections, a
Snowflake MCP server (not connected), a dbt profile"):

| Source | How to detect | Use |
|---|---|---|
| `snow` / Python connector connections (`~/.snowflake/connections.toml`, `config.toml`; `$SNOWFLAKE_HOME` when set) | doctor's `snow-connection` and `snow-key-file` rows; names via the filtered list below | Probes; local preview. `st.connection("snowflake")` and the Python connector read this same store. |
| `SNOWFLAKE_DEFAULT_CONNECTION_NAME` | whether it is set, and its name | Which connection preview actually uses |
| Snowflake MCP server, connected | a tool in this session that runs Snowflake SQL | Probes (fallback) |
| Snowflake MCP server, configured but not connected | the session's server status (still connecting, timed out, needs auth) and server names in the repo's `.mcp.json`, names only: `python3 -c "import json; print(list(json.load(open('.mcp.json')).get('mcpServers', {})))"` | Report "present but unavailable" with the reason; never treat it as absent |
| dbt `profiles.yml` target of `type: snowflake`, or a dbt project in the repo | only the `database`, `schema`, `role` keys | Signals for the proposals |
| `SNOWFLAKE_*` environment variables | variable names only: `env \| cut -d= -f1 \| grep '^SNOWFLAKE_'` | A custom connection exists; offer to turn it into a `snow` connection |
| Legacy SnowSQL config `~/.snowsql/config` | section names only: `grep -o '^\[connections\.[^]]*\]' ~/.snowsql/config` (never Read the file: it can hold passwords) | Same offer |

Only `snow` connections and a connected MCP server can run probes; the rest are signals.

Every user arrives with a different setup: a `snow` connection from a past tutorial, a Snowflake
MCP server in their agent, a dbt profile, several accounts, or nothing at all. Work out which
before asking anything. A `snow` connection is the recommended path, because local preview reads
it and `--connection` can take the account from it without anyone typing the account into chat;
it does not have to be the default one (`--connection` takes any name), and the user may prefer
to keep their current setup. Local preview reads the default connection, so a non-default one
then needs the per-app `secrets.toml` override from step 3.

| What you find | What to do |
|---|---|
| A default `snow` connection (`snow-key-file` in `streamsnow doctor --format json` names it) | Use it. Confirm with the user that it is the account these apps are for. |
| `snow` connections, none of them default | List the names only: `snow connection list --format json \| python3 -c "import json,sys; [print(r.get('connection_name'), r.get('is_default')) for r in json.load(sys.stdin)]"`. Ask which one is this account. Making it the default (`snow connection set-default <name>`) repoints every tool that reads the default, so ask before running it. |
| No `snow` connection, but a Snowflake MCP server or a dbt profile | Investigate through those (2b). Then offer to add a `snow` connection as below, since local preview needs one. |
| No Snowflake access on this machine | Ask (Stage 2): "Should I set up the connection for you, or would you rather do it yourself and I guide you?" Then ask how they sign in: SSO (`--authenticator externalbrowser`), a key pair (`--private-key-file <path>` with `--authenticator SNOWFLAKE_JWT`), or a programmatic access token. **I set it up for you:** for a token, they save it to a file themselves and you pass `--authenticator PROGRAMMATIC_ACCESS_TOKEN --token-file-path <path>` (never take the token in chat). They give the account identifier (Snowsight's account menu, bottom left, under account details) and username as answers; run `snow connection add --connection-name <slug> --account <account> --user <user> --authenticator <method> --default --no-interactive` (drop `--default` to leave their current default alone), then `snow connection test -c <slug>`, and tell them to approve in the browser window that opens. The account and username then appear in the chat; they are not secrets. **I'll guide you:** have them run `snow connection add --connection-name <slug> --default` in their own terminal with only the sign-in flag, and explain each prompt it asks. Either way, re-run the investigation with the new connection. |
| No Snowflake account | Point them to a Snowflake trial. Trial accounts have no compute pools, so expect the `warehouse` runtime. |

Never ask for a password, token or key in chat, and never open or edit the connection files. Don't
recommend password-only auth (Snowflake's MFA rollout retires it); if the user chooses it anyway,
say so once and leave it to them. If the user would rather not set up a connection now, carry on
with whatever access exists and fall back to asking.

### 2b · Investigate (read-only)

Find the answers before asking anything. Use the source the user named, or else whatever
Snowflake access this machine and this agent session have, in this order, and say which source
and role each finding came from:

1. **The `snow` CLI's default connection** (it reads `~/.snowflake/connections.toml` and
   `config.toml`). Its name is `detail.connection_name` of the `snow-key-file` check in
   `streamsnow doctor --format json` (present whenever a default connection exists). Run each probe
   as `snow sql -c <connection> --format json -q '<query>'`, in single quotes: the probes contain
   `"name"` and `$1`, which double quotes would hand to the shell. This is the preferred source: it is
   the connection local preview reads, and `--connection` can take the account from it unseen.
2. **A Snowflake MCP server already connected to this agent session** (a tool that runs Snowflake
   SQL). Run the same probes through it. It is the fallback when `snow` is missing, broken or has
   no default connection, and a second opinion when the `snow` role sees too little.
3. **Other local Snowflake config, for signals only**: a dbt `profiles.yml` target of
   `type: snowflake`, or a dbt project in this repo. Read only the `database`, `schema` and `role`
   keys (a dbt target database usually is the curated one; `models/marts/` and similar folders
   name the curated schemas). Never read or print passwords, tokens, key paths or whole files.

Before trusting an empty result, run `SELECT CURRENT_ROLE()` on that source. An empty `SHOW` under
`PUBLIC` or another low role means "not visible to this role", not "does not exist": try the next
source, or ask that question instead of proposing from absence.

**Never print** `snow connection list`, `claude mcp list` (or another agent's MCP listing), MCP
config files, connection files, `profiles.yml`, or `SNOWFLAKE_*` environment values: any of them
can show the account or a credential. Read named keys through a filter instead.

**Probes.** This is a method, not a recipe: adapt it to whatever the user's tools accept. The
`->>` pipe keeps only the columns you need, so owners and share origins stay off screen. Some
tools refuse `SHOW` or the pipe form; then use the `INFORMATION_SCHEMA` fallback, which any
read-only SQL tool accepts.

| Question | Probe | When `SHOW` is refused |
|---|---|---|
| Runtime | `SHOW COMPUTE POOLS ->> SELECT "name", "state" FROM $1` | No equivalent: try another source, or ask |
| Account | none (see below the table) | |
| Database | `SHOW DATABASES ->> SELECT "name", "kind", "comment" FROM $1` | `SELECT database_name, type, comment FROM SNOWFLAKE.INFORMATION_SCHEMA.DATABASES` |
| Schemas | `SHOW SCHEMAS IN DATABASE <db> ->> SELECT "name", "comment" FROM $1` | `SELECT schema_name, comment FROM <db>.INFORMATION_SCHEMA.SCHEMATA` |
| What schemas hold | `SELECT table_schema, COUNT(*) FROM <db>.INFORMATION_SCHEMA.TABLES WHERE table_schema <> 'INFORMATION_SCHEMA' GROUP BY 1` | (already a SELECT) |
| Deploy source | `SHOW GIT REPOSITORIES IN ACCOUNT ->> SELECT "database_name", "schema_name", "name" FROM $1` | No equivalent: `stage-copy` is the default either way |

Account: with a `snow` connection, pass `--connection <name>`; the CLI reads the account and
never prints it. With only an MCP server,
`SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME()` returns a usable
identifier, but it then appears on screen: ask before running it, or let the user type it.

**Reading the evidence.** Every team names things differently, so weigh signals in this order,
strongest first, and treat names as the weakest:

1. What the user has said about the app and its data.
2. Comments on databases and schemas.
3. What the objects hold: table and view counts (never propose an empty schema), and, with the
   user's OK, table names. A narrowly scoped role is evidence too: its grants
   (`SHOW GRANTS TO ROLE <current role> ->> SELECT "privilege", "granted_on", "name" FROM $1`)
   show which data someone chose to expose. A broad role's grants say little.
4. The user's own tooling: a dbt project or profile names its target database and its layers.
5. Names, as hints only. Common conventions (`MARTS`, `REPORTING` or `GOLD` for curated data;
   `RAW`, `STAGING`, `LANDING`, `BRONZE` or `DEV` for raw and working layers) are examples, not
   rules; say when a proposal rests on names alone.

A few facts hold whatever the naming:

- **Some databases cannot be the governed one**: a share (`kind` `IMPORTED DATABASE`) takes
  `GRANT IMPORTED PRIVILEGES`, not the per-schema `SELECT` grants `deploy-setup --admin` writes;
  a `PERSONAL DATABASE` belongs to one user; an `APPLICATION` database (such as `SNOWFLAKE`) and
  the StreamSnow app database (`STREAMSNOW_APPS` by default) hold no reporting data.
- **Runtime**: a visible compute pool means `container` works; propose it, defaulting to
  `SYSTEM_COMPUTE_POOL_CPU` when listed. No pool visible to a role that can see them suggests a
  trial account, which only runs `warehouse`; a paid account's admin can still grant a pool.
- **Deploy source**: propose `stage-copy` unless a GIT REPOSITORY already exists; the user can
  choose `git-repository` either way. When it comes up, say what it adds: an API integration
  (ACCOUNTADMIN creates it), a GitHub token stored as a Snowflake secret, and Snowflake needing
  network access to GitHub.
- **Deny list**: propose the schemas the evidence marks as raw, staging or working layers, not
  the `RAW,STAGING` default. When there are none, omit `--deny-schemas` so the default stands,
  and say it guards names that do not exist yet. A layer the evidence cannot place (an
  intermediate one, say) goes in neither list until the user decides. Never put a schema in both.
- **Two or more plausible candidates** for any answer: show them and ask.

Different sources run as different roles and can see different databases (a personal `snow`
connection may see a handful where an admin-role MCP sees many more). When they disagree, show
what each role sees rather than picking one silently. Proposing from a broader role is fine: the
allowed schemas still reach apps only through the grants `deploy-setup --admin` writes.

These are SHOW and SELECT-over-SHOW (or `INFORMATION_SCHEMA`) only. **Never run DDL, grants, or anything that writes**, and
never switch roles to get more visibility. A probe that errors (no privilege, no source can see
it) or returns nothing to choose from is not a failure: that question falls back to asking the
user plainly, with no proposal, and you say why. With no usable source at all, ask all five and
pass `--account` with the locator the user gives.

### 2c · Propose, confirm, run

Ask every answer, one question each, even when the investigation found a clear one (the
question rule in [SKILL.md](SKILL.md) Stage 2). Before each question, explain the setting in two
or three plain lines: what it is, what it controls, and what changing it later costs. Then ask,
with the detected value as the recommended first option, its reason and source in one line, and
the other candidates after it. Where nothing was found or two candidates are plausible, say so
and ask with no recommendation. Order: runtime, database, allowed schemas, denied schemas, deploy
source, plus the connection when §2a has not already confirmed it. The explanations come from the
facts above (runtime, deploy source and deny list), and for the schema lists: `deploy-setup
--admin` grants the CI role SELECT on exactly the allowed schemas, and deployed apps run with
their owner's rights (the CI role), so the allowed list is the data boundary for every viewer; the
denied list is what `streamsnow check schema-refs` blocks in app code. Ask the allowed and denied
lists as two questions, because they are two different boundaries. Then write the config with
the confirmed answers:

```
streamsnow configure --runtime container --connection <name> \
  --database ANALYTICS --schemas MARTS,REPORTING --deny-schemas RAW,STG_CRM \
  --deploy-source stage-copy
```

With all five answers passed, no prompt fires. Before writing the repo files, explain the
defaults the wizard did not ask about, as a bulleted list with one bullet per thing that applies,
then ask one question: keep them, or change some. Each bullet gives the proposed value, what it
is, what uses it, whether the admin script creates it, and when a team would change it (a team
may already have its own warehouse, roles or naming). Cover these, and only the ones that apply:

- **Project name**: derived from the folder; labels the config and generated files, and is not
  created in Snowflake.
- **App database and schema** (`STREAMSNOW_APPS.DASHBOARDS` by default): where the deployed
  Streamlit apps live; the admin script creates them.
- **Stage database and schema**: where `stage-copy` deploys put the app code; they default to the
  app database and schema.
- **Warehouse** (`STREAMSNOW_WH` by default): the compute apps query with; the admin script
  creates a small one that suspends after 60 seconds. Reuse an existing warehouse if your team has
  one.
- **CI role and viewer role** (`STREAMSNOW_DEPLOY_ROLE`, `STREAMSNOW_VIEWER_ROLE`): the first
  deploys from CI and owns the apps, the second is what people viewing an app and local preview
  use; the admin script creates both.
- **Compute pool and external access integration** (container runtime only;
  `SYSTEM_COMPUTE_POOL_CPU` and `PYPI_ACCESS_INTEGRATION` by default): the pool Snowflake
  pre-provisions to run the app, and the permission that lets the build install packages from
  PyPI.
- **Git repository, API integration and secret names** (only when the deploy source is
  `git-repository`): placeholders to confirm before the first deploy.

The question's "change some" option takes the names to change; values changed after `init` need
`streamsnow update --apply` to re-render the files. Change the values the user asks for in
`streamsnow.config.yaml`, keeping its keys and comments. Nothing needs changing for a first run;
each value carries a comment saying when to. Then run `streamsnow init --no-starter-app`: it
reuses that config and renders `AGENTS.md`, CI and the rest from the final values. (Editing
defaults after `init` leaves those files naming the old values until
`streamsnow update --apply` re-renders them.) `streamsnow init` takes the same flags, for a user
who wants no review of the defaults. `--connection` reads the account from that
connection and makes it `snowflake.connection_name`; use `--account <locator>` only when there is
no `snow` connection (MCP-only or nothing at all). Local preview still needs a `snow` connection
later, which step 3 covers. If `--connection` exits 2 (the connection names no account, or `snow`
cannot list connections), ask for the locator and pass `--account` instead. To change answers
later, run `streamsnow configure` (interactive, prefilled from the current file) or
`streamsnow init --no-starter-app --reconfigure` with the changed flags; answer flags on an
existing config without `--reconfigure` exit 2 rather than being ignored. When the deploy source
is `git-repository`, the written `deploy.git_repository_fqn` is a placeholder: propose setting it
to the repository the probe found.


- **Don't hand-author `streamsnow.config.yaml` from scratch**: the wizard owns its shape, and the
  answer flags are its supported non-interactive path. Editing values in the file it wrote is
  fine; that is what it is for.
- **Not in Claude Code?** After `init`, `streamsnow agent-skills install --agent codex` copies these
  skills into the repo's `.agents/skills/`, where every teammate's Codex finds them; commit it.
- `streamsnow init` without the flag also scaffolds an `example-dashboard` starter app. That is
  the CLI-only path; with Claude the real app comes from `/build-app`, so pass the flag.
- The first deploy needs one-time Snowflake objects that an admin creates: see §2d.

## 2d · Snowflake admin setup (once per account)

The first deploy needs objects most people cannot create: the app database and schema, a
warehouse, the CI and viewer roles, a CI service user and the grants that tie them together.
[docs/deploy-setup.md](../../docs/deploy-setup.md) §0 has the full table.

1. **Check, read-only** (Stage 1). Through the user's own connection, run
   `SHOW DATABASES LIKE '<objects.app_database>'` and `SHOW ROLES LIKE '<roles.ci_role>'`, with
   names from the config, or from the proposed answers when there is no config yet. Both found:
   **confirmed**, skip to §2e. Anything else is **not confirmed, never "missing"**: a
   low-privilege role may not see these objects even when they exist.
2. **Explain, then ask** (Stage 2). Before asking, show what the admin setup script is for, as
   bullets with the real names from the config:
   - **What it is**: one SQL script, written from `streamsnow.config.yaml`, that an admin runs
     once per Snowflake account. Snowflake only lets admin roles create these objects, which is
     why StreamSnow cannot do it with the user's everyday role.
   - **App database and schema**: where deployed apps live.
   - **Warehouse**: the compute apps query with.
   - **CI role**: what the deploy workflow runs as; it owns the apps.
   - **Viewer role**: what people viewing an app run as. It gets no data grants unless the admin
     uncomments them, so local preview uses a role whose data reads match the CI role's.
   - **CI service user**: the account the deploy workflow signs in as, with a key pair and no
     password.
   - **Grants**: the CI role gets SELECT on exactly the allowed schemas, which is what makes the
     allowed list the data boundary.
   - **Container runtime only**: the PyPI access integration and the compute pool permission.
   - **Safe to re-run**, and Claude never runs it. `streamsnow deploy-setup --teardown` prints
     a start-fresh cleanup the admin must review line by line before running.

   Then ask: "Has your Snowflake admin run the StreamSnow setup script? If not, who runs it: you,
   or someone else?"

   **Which of your roles get access.** Unless step 1 confirmed the setup, ask this too. The
   script gives the viewer role only to whoever runs it, and the admin who runs it is often a
   different login from the one building the apps, so the user's own connection (and you) would
   otherwise see none of the new objects and step 1 could never confirm them. List the roles the
   user's connection can use, read-only: `SELECT CURRENT_AVAILABLE_ROLES()` and
   `SELECT CURRENT_ROLE()`. Explain in one line that each chosen role gets the viewer role, so
   everyone holding it can open the apps and see the app database and warehouse, and none of them
   gets the data or deploy rights. Ask which roles get it (multiple choice), recommending the
   connection's current role. Leave out `PUBLIC`, the system roles (`ACCOUNTADMIN`,
   `SECURITYADMIN`, `SYSADMIN`, `USERADMIN`, `ORGADMIN`) and StreamSnow's own two roles: the CLI
   refuses them. Also leave out a role whose name is not all capital letters, digits, `_` or `$`
   (the CLI writes names unquoted). Recommend the current role only when it is not one of those;
   otherwise recommend none. "None" is a valid answer.
3. **CI key.** Run `streamsnow ci-key create`. It writes the key pair and one file per secret to
   `~/.streamsnow-ci`, prints only file names and a fingerprint, and never overwrites a key. Tell
   the user, in one line, to save a copy of the `.p8` somewhere safe such as a password manager
   themselves (you never open it; a lost key means rotating).
4. **The admin file.** Run these as three separate commands, in this order, with no chaining,
   piping or grouping: the key guard denies any command that names `~/.streamsnow-ci` unless it
   starts with `streamsnow ci-key` or `streamsnow deploy-setup` and has nothing else attached.
   1. `mkdir -p .internal`
   2. `streamsnow deploy-setup --admin --public-key-file ~/.streamsnow-ci/streamsnow_ci_rsa_key.pub > .internal/admin-setup.sql`
      on a line of its own, adding `--viewer-role <ROLE>` once for each role chosen in step 2.
   3. `git check-ignore -q .internal/admin-setup.sql`. Repos set up on 0.7.1 or later already
      ignore `.internal/`; if this check fails, add `.internal/` to `.gitignore` so the file is
      never committed.

   The file runs unedited and is safe to re-run; `streamsnow deploy-setup --teardown` prints a
   start-fresh cleanup to review.
5. **Hand it off.** You never run the admin SQL, whoever the user is. Give the explanation from
   step 2 and the steps below, nothing more: do not open or quote the admin file, and do not add
   your own warnings about its contents (line numbers, statement types, object names). The
   script is documented, and extra commentary about it only worries the person about a script
   they are meant to run unedited.
   - **The user is the admin:** copy it for Snowsight: `pbcopy < .internal/admin-setup.sql` on
     macOS; `wl-copy < .internal/admin-setup.sql` or
     `xclip -selection clipboard < .internal/admin-setup.sql` on Linux when present; otherwise
     open the file for them. Then give the steps: in Snowsight, open a new SQL worksheet, paste,
     and choose Run All. When they say it ran, repeat step 1.
   - **Someone else is the admin:** write a short message the user can forward, naming
     `.internal/admin-setup.sql` and linking to docs/deploy-setup.md §0. Record "waiting on your
     Snowflake admin".
6. **Not blocking.** Until step 1 confirms, skip the secrets in §2e and finish at "ready to build
   and preview". Re-running `/onboard` repeats the check.

## 2e · Shared repo settings (once per repo; a teammate usually finds them done)

**The plugin for everyone.** Read `.claude/settings.json`. If it does not enable
`streamsnow@streamsnow`, explain: "Saving StreamSnow into the repo's Claude settings means
everyone who opens this repo is offered the same tools automatically." Then run
`claude plugin marketplace add --scope project kyle-chalmers/streamsnow` and
`claude plugin install --scope project streamsnow@streamsnow`, and offer to commit
`.claude/settings.json`.

**Prove what CI will see (optional, ask first).** Offer this before `streamsnow ci-key push`
below: the push switches the deploy job on, and this catches a bad CI identity before the first
merge deploys. Skip it while §2d is not confirmed or the key files from `ci-key create` are not
on this machine. `streamsnow ci-key verify` signs in as the CI service user the way the deploy
job does and runs read-only checks: the CI role, the warehouse, the app schema, the grants the
admin script gives the CI role, and one `LIMIT 0` read, each with secondary roles off so it
proves the CI role alone (deployed apps query with that role's owner's rights). Before running
it, tell the user plainly and ask:
- it signs in from this machine with the production CI key;
- the sign-in shows in the CI user's login history in Snowflake;
- if the account has a network policy that only admits the CI runners, it will be refused,
  which means the policy is doing its job and the deploy job will still sign in from CI;
- it is meant to run once, right after the admin setup.

On a yes, run `streamsnow ci-key verify --object <DB.SCHEMA.TABLE>` with a table or view in an
allowed governance schema that the first app will read (leave `--object` off if there is none
yet). It prints each check by object name and never the key, account or user. Exit 1 names
what failed: a missing grant goes back to the admin as one line naming the grant; a sign-in
failure usually means the admin registered a different public key, so compare the fingerprint
from `ci-key create` with `RSA_PUBLIC_KEY_FP` in `DESC USER`. Exit 2 is a tool error with no
results printed: a missing or unreadable secret file, a file that differs from the config, a
bad config or `--object`, no `snow`, or a `snow` call that could not start, timed out or
printed unreadable output. On a no, skip it: the first deploy checks the same things.

**Deploy secrets on GitHub.** Doctor's `ci-secrets` row lists which are missing. Explain: "The
deploy workflow signs in to Snowflake with these GitHub secrets; until they exist, merging
deploys nothing." Skip this when the row says "not checked" because the user cannot list secrets
(only someone with access to the repo's settings can set them), when there is no GitHub remote
yet, or while §2d is not confirmed. Otherwise run `streamsnow ci-key push`. If it says the key
files are missing (a teammate's machine, or the admin registered someone else's key), never run
`ci-key create` on your own: a new key would not match the public key Snowflake holds, and every
deploy would fail. Either push from the machine that made the key, or do §2d steps 3 to 5 so the
admin registers the new `.pub`. When it runs, it sets
`SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY_RAW`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE` and then
`SNOWFLAKE_ACCOUNT`, each straight from its file to `gh` on stdin, and prints only names.
Setting `SNOWFLAKE_ACCOUNT` switches the deploy job on, so tell the user the next merge to
`main` will deploy. If it stops partway, it names what failed and leaves `SNOWFLAKE_ACCOUNT`
unset; fix the cause and run it again.

If `ci-key push` refuses a secret file by name (empty, not UTF-8 text, contains a NUL byte, or
differs from the config), offer to walk the user through replacing it. The key guard keeps you
out of `~/.streamsnow-ci`, so the user runs the delete. Name the file, say that it is a value
the config can rebuild and not the key itself, and give them the one command to run in their
own terminal: `rm ~/.streamsnow-ci/secrets/<NAME>` (with `--dir`, use that folder instead).
Then run `streamsnow ci-key create`, which rewrites the missing file from the config and keeps
the existing key pair, and run `streamsnow ci-key push` again. If the refused file is
`SNOWFLAKE_PRIVATE_KEY_RAW`, stop instead: that file points at the key itself, and replacing
the key means the admin registers a new public key (§2d steps 3 to 5).

**Delete merged branches on GitHub.** Skip this when there is no GitHub remote yet or `gh` is
not signed in; it does not wait on §2d. Find the repo with
`gh repo view --json nameWithOwner --jq .nameWithOwner`, then read the setting first:
`gh api repos/<owner>/<repo> --jq .delete_branch_on_merge`.
- `true`: already on. Say so in one line and move on.
- `false`: ask one yes/no question: "Turn on GitHub's automatic deletion of merged branches for
  this repo? It changes a repo setting: once a pull request merges, GitHub deletes its branch, so
  nobody reuses a branch whose work was squash-merged, which can silently undo changes
  ([_shared/sync-with-main.md](../_shared/sync-with-main.md))." On yes, run
  `gh repo edit <owner>/<repo> --delete-branch-on-merge` and re-read the setting to confirm it
  reads `true`. On no, mark it skipped. Never change it without that yes.
- Empty output (GitHub returns the setting only to repo admins), or `gh repo edit` is refused:
  only a repo admin can change it. Say so in one line, name where an admin turns it on (the
  repo's Settings, General, "Automatically delete head branches"), and move on. Nothing else
  depends on it.

## 3 · Connection (one store, owned by the user)

**A non-default connection needs one more step.** `snowflake.connection_name` may name a
connection that is not the machine's default (the user kept their own default in 2a). The
`snow-connection` check still passes, but local preview reads the default connection, so without
this step it would open the default account. Before the first preview, let the user pick one:
make it the default (`snow connection set-default <name>`, which repoints every tool that reads
the default); set `SNOWFLAKE_DEFAULT_CONNECTION_NAME=<name>` in the shell that runs preview (the
Python connector reads it, and nothing else changes); or copy an app's
`.streamlit/secrets.toml.example` to `secrets.toml` and fill it in.

**Check before adding anything.** When the machine already has a default `snow` connection (a
prior tutorial, another project), the wizard writes that name into `snowflake.connection_name`,
and `init`'s `Next:` block says there is nothing to add. If `streamsnow doctor --format json`
already reads `snow-connection: ok: true`, skip to the key-file check below: **do not** have the
user run `snow connection add … --default`, which would add a second connection and repoint the
default every other tool reads. If the check fails but its hint names an existing default
connection, the usual fix is to set `snowflake.connection_name` to that name (confirm with the
user first) rather than create a new one.

A connection created in 2a is already the default, so this step only confirms it. When the user
skipped creating one in 2a, `streamsnow init` (and `configure`) print the exact
`snow connection add … --default` command for the account.
Offer §2a's choice (set it up for them, or guide them); never ask for credentials in chat. That writes
the `snow` CLI's `connections.toml`, which `st.connection("snowflake")` reads locally, so it is the
only place account details get typed. Then re-run `streamsnow doctor --format json` and confirm the
`snow-connection` check reads `ok: true`.

**Key-pair connections:** a `snow-key-file` warning means the default connection uses key-pair
auth with a key name only the `snow` CLI reads (the legacy `private_key_path`). `snow sql` works,
and local preview then crashes on the first page load with `TypeError: Expected bytes,
RSAPrivateKey, ...`. Have the user rename `private_key_path` to `private_key_file` in that
connection's entry (a passphrase goes in `private_key_file_pwd`); both `snow` and the Python
connector read that name. Never open or edit the connection file yourself. The per-app `apps/<slug>/.streamlit/secrets.toml`
(gitignored) is an optional override for an app that needs a different role or warehouse — offer
it only when asked. Two classic traps either way:

- `account` is a locator (`ab12345.us-west-2`), **not** the full `*.snowflakecomputing.com`
  hostname — the connector appends the suffix, and a doubled one fails auth.
- Preview with a role whose data reads match the CI role's (deployed apps run with owner's rights):
  the viewer role with the opt-in data grants from `deploy-setup --admin` uncommented, or a developer
  role with the same reads. Not a broad personal role, which hides missing grants locally that then
  ship as empty dashboards. And never a password:
  Snowflake's MFA rollout retires password-only auth (`externalbrowser` or a programmatic access
  token — see docs/snowflake-docs.md).

## Troubleshooting

- **`streamsnow: command not found`** — not installed (§0b) or the tool bin dir isn't on PATH
  yet; re-open the shell.
- **doctor keeps reporting Python too old** — a newer Python exists but isn't first on PATH.
- **A check flips back to red** — read its `hint` and `detail` verbatim; don't advance past a
  `required` failure.
- **Preview can't connect** — no default `snow` connection (the `snow-connection` doctor check
  says so) or, if a `secrets.toml` override exists, its account format / role / warehouse grant.
  Print the connection error verbatim and have the user recheck.
- **Preview dies with `TypeError: Expected bytes, RSAPrivateKey, ...`**: the key-pair key is named
  `private_key_path`; see the key-pair note in step 3 (`streamsnow preview logs` prints the same
  remedy).

## Done → next step

Everything green, config written, and the governed repo files on disk (`ls AGENTS.md .gitignore
.pre-commit-config.yaml` answers). Branch on intent, and let the user choose:

```
Building a new dashboard?          → /build-app
Documenting an existing app?       → /build-app --spec <slug>
Porting an external Streamlit app? → /migrate-app
Just want to run one locally?      → /preview-app <slug>
```

Onboarding never fills in or reads credentials or keys, never edits existing CI or deploy config
(`init` only writes repo files that are missing), and installs nothing and changes no GitHub
setting without a one-line explanation and the user's yes.
