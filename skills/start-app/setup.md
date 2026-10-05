# Setup mode — first-time machine + repo configuration

Get a fresh machine and repo ready to build and preview apps, for the person creating the repo and
for every teammate who clones it later. `streamsnow doctor` is the source of truth: don't
re-derive prerequisites by hand (no `which python`, no version greps).

**You do the setup; the user approves.** The person running this may know nothing about
StreamSnow or whoever sent it to them, so trust comes from saying plainly what you are about to do
and why. Before every install or change, say in at most two short lines **what** it is and **why
they need it** (the table in §1 has the wording), show the exact command, wait for one "yes", run
it, re-run `streamsnow doctor --format json`, and report the result in one line. Example:

> Next: Node.js, a small runtime StreamSnow uses to drive a browser that clicks through your app
> and screenshots each page, so you see problems before you ship. Command: `brew install node`. OK?

Plain words, one step at a time, no walls of text. If they decline, mark it skipped and move on.

Run everything yourself except the few steps that need the user's own identity or an admin. Hand
those over with a one-line reason:
- browser sign-ins (`snow connection add`, `gh auth login`): only they can sign in as themselves;
- `/reload-plugins`: a slash command only the user can type;
- the Snowflake admin script (`streamsnow deploy-setup --admin`): it needs admin rights;
- anything that asks for their computer password (`sudo` on Linux/WSL): your shell cannot answer
  that prompt, so give them the command; prefer installs that need no password (uv, nvm).

The CI key is handled only by `streamsnow ci-key create` and `streamsnow ci-key push` (§2d). The
plugin's key guard blocks every other tool call that names `~/.streamsnow-ci`, so a blocked call
there is expected: never try another way to read a key or secret file.

## 0 · Windows: use WSL

If your environment reports a native Windows platform (`win32`; inside WSL it reports `linux`),
stop before installing anything. Say, in two lines: StreamSnow runs inside WSL (Windows Subsystem
for Linux), Microsoft's built-in Linux layer for Windows, because its local preview and safety
hooks only work on macOS and Linux today; it is a one-time setup and the user's apps work the same
there. Then walk them through it: open PowerShell as administrator, run `wsl --install`, restart
when asked, open the new Ubuntu app, install Claude Code inside it, and run `/start-app --setup`
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

Report in one line ("5 checks passed, 2 need attention"), then walk each `ok: false` check one at a
time — propose the fix (start from the check's own `hint`; the table below gives per-OS commands),
run it on confirmation, then re-run `streamsnow doctor --format json` and confirm that check now
reads `ok: true` before moving on. Never batch installs. `level` decides severity: a `required`
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
plugin's bundled browser tool runs through `npx`, and without it every UI walkthrough is skipped.
`ci-secrets` belongs to §2d. `gh` is optional here and required later by `/ship-app`. `container-python` warns in a
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
| Node.js 20+ (optional, recommended) | runs the browser tool that clicks through the app and screenshots each page | `brew install node` | nvm (`nvm install --lts`); distro packages are often too old |
| GitHub CLI `gh` (optional; `/ship-app` needs it) | opens pull requests and checks the deploy settings on GitHub | `brew install gh`, then `gh auth login` (user signs in) | distro package, then `gh auth login` |

After the repo is configured, run `pre-commit install` yourself (doctor's `pre-commit-hook` row
confirms it). In git worktrees a repo-managed `core.hooksPath` can make it refuse; confirm with the
user before unsetting it, since it is sometimes intentional. If another tool's hook is already
there, `pre-commit install` keeps it as `pre-commit.legacy` and still runs it.

### 1b · Browser check (advisory, never blocks)

StreamSnow ships a Playwright browser tool with the plugin, used to walk through each page of a
running app. Confirm it works now rather than finding out later when a review silently skips it:

- The first start downloads the tool, so it can still be connecting: search your tools for
  `browser_navigate` (a tool search waits for servers that are still connecting) before deciding
  it is missing.
- If `browser_*` tools are available, navigate to `about:blank`, then close the browser. Report
  "browser tool works" in one line. If it fails with "is not installed" or "Executable doesn't
  exist", explain that the browser itself needs a one-time download, run `npx @playwright/mcp@<version> install-browser <name>`
  with the browser name from the error and the version from the plugin's `.mcp.json` (the error
  prints the command without a version, which would fetch a different release), and retry once.
- If they are not visible and doctor's `node` row is not ok, install Node first (see the table).
  Then ask the user to type `/reload-plugins` so the plugin's browser tool starts, and retry.
- Still failing after that: say the UI walkthrough will be skipped until it is fixed, point to
  `docs/troubleshooting.md`, and continue. Nothing else depends on it.

## 2 · Repo configuration + governed repo files

First decide which case this is:

- The repo **already has a `streamsnow.config.yaml`** (a teammate cloning a set-up repo): read
  doctor's `repo-files` row. When it is ok, there is nothing to scaffold: skip init and adopt, make
  sure `pre-commit install` ran (§1), then go to §2d and §3. When it lists missing files, run
  `streamsnow init --no-starter-app`; it reuses the config and writes only the missing files.
  Never treat the config alone as "already set up".
- The repo **already has Streamlit apps or its own agent commands or skills** but no config: stop, that's
  [adopt mode](adopt.md), which maps onto what exists instead of scaffolding.
- Otherwise (an empty repo, or one with no `apps/` yet), the setup verb is
  `streamsnow init --no-starter-app`. It runs the config wizard (or reuses an existing
  `streamsnow.config.yaml`) and then writes the governed repo files: `AGENTS.md`, `CLAUDE.md`,
  `.gitignore`, `.pre-commit-config.yaml`, `.github/workflows/`, `README.md` and
  `deploy/tombstones.yml`. It writes no example app; `/start-app` scaffolds the real one next
  with `streamsnow new`. **Never run only `streamsnow configure` here** (`configure` followed by
  `init`, as in 2c, is fine): `configure` writes the
  config file and nothing else, and `streamsnow new` writes app files only, so a repo set up that
  way has no hooks, no CI and no `.gitignore` (an app's `.streamlit/secrets.toml` could then be
  committed). `streamsnow new` warns when those files are missing; the fix is the same command.

When `streamsnow.config.yaml` already exists, run `streamsnow init --no-starter-app` as is: it
reuses the file and writes only the missing repo files. Skip the proposals below.

The wizard asks **at most 5 questions**: runtime, Snowflake account, the database apps query, the
allowed schemas, and the deploy source. Everything else (project name, roles, warehouse, schema
names, container objects) is written as a sensible default with an inline comment saying when to
change it; the file is the editing surface. Don't make the user answer those questions cold:
investigate, propose, confirm, then pass the confirmed answers as flags.

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
| No Snowflake access on this machine | Offer to help set it up. Ask how they sign in: SSO (`--authenticator externalbrowser`), a key pair (`--private-key-file <path>` with `--authenticator SNOWFLAKE_JWT`), or a programmatic access token. Then have them run `snow connection add --connection-name <slug> --default` (any name they like; drop `--default` to leave their current default alone) in their own terminal, adding only that sign-in flag; it prompts for the account identifier, user and the rest, so nothing is typed into chat. The account identifier is in Snowsight's account menu (bottom left) under the account details. Then have them run `snow connection test -c <slug>`, which may open a browser. |
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

Ask only what the investigation could not settle. Show one table of all five answers, each with a
one-line reason and the source it came from, and mark each row **found** (one clear answer from
the evidence) or **needs you** (two plausible candidates, nothing visible, or a judgment call).
Ask the **needs you** rows as direct questions; one "yes" confirms every **found** row, and the
user can still change any of them inline ("schemas: MARTS only" is enough). The allowed-schema
list is always shown, even when found, because it is the data boundary. Explain the schema lists when you
show them: `deploy-setup --admin` grants the CI role SELECT on exactly the allowed schemas, and
deployed apps run with their owner's rights (the CI role), so the allowed list is the data
boundary for every viewer; the denied list is what `streamsnow check schema-refs` blocks in app
code. Then write the config with the confirmed answers:

```
streamsnow configure --runtime container --connection <name> \
  --database ANALYTICS --schemas MARTS,REPORTING --deny-schemas RAW,STG_CRM \
  --deploy-source stage-copy
```

With all five answers passed, no prompt fires. Before writing the repo files, show the defaults
the wizard did not ask about (project name, app database and schema, warehouse, CI and viewer
roles, compute pool) in a short list and ask whether any should change: a team may already have
its own warehouse, roles or naming. Change the values the user asks for in
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
  the CLI-only path; in this skill the real app comes from `/start-app`, so pass the flag.
- The first deploy needs one-time Snowflake objects (database, schema, warehouse, roles, a CI
  service user, grants). `streamsnow deploy-setup --admin` prints the reviewable DDL; surface it
  for the user's Snowflake admin, never run it yourself. You run `streamsnow ci-key create` for the
  CI key pair (it prints only file names and a fingerprint) and tell the user, in one line, to
  save a copy of the `.p8` somewhere safe such as a password manager themselves (you never open
  it; a lost key means rotating), then `deploy-setup --admin
  --public-key-file <its .pub>` so the file runs unedited; it is safe to re-run, and
  `deploy-setup --teardown` prints the start-fresh reverse.

## 2d · Shared repo settings (once per repo; a teammate usually finds them done)

**The plugin for everyone.** Read `.claude/settings.json`. If it does not enable
`streamsnow@streamsnow`, explain: "Saving StreamSnow into the repo's Claude settings means
everyone who opens this repo is offered the same tools automatically." Then run
`claude plugin marketplace add --scope project kyle-chalmers/streamsnow` and
`claude plugin install --scope project streamsnow@streamsnow`, and offer to commit
`.claude/settings.json`.

**Deploy secrets on GitHub.** Doctor's `ci-secrets` row lists which are missing. Explain: "The
deploy workflow signs in to Snowflake with these GitHub secrets; until they exist, merging deploys
nothing." Skip this when the row says "not checked" because the user cannot list secrets (only
someone with access to the repo's settings can set them), or when there is no GitHub remote yet.
It needs the CI service user from the admin script (§2): if the admin has not run it yet, say
"waiting on your Snowflake admin" and come back on the next run.
Order matters: `SNOWFLAKE_ACCOUNT` switches the deploy job on, so it goes last, or every merge
fails at sign-in while the key is still missing.
Once the admin has run the script, run `streamsnow ci-key push`. It sets `SNOWFLAKE_USER`,
`SNOWFLAKE_PRIVATE_KEY_RAW`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE` and then `SNOWFLAKE_ACCOUNT`,
each straight from its file to `gh` on stdin, and prints only names. Setting `SNOWFLAKE_ACCOUNT`
switches the deploy job on, so tell the user the next merge to `main` will deploy. If it stops
partway, it names what failed and leaves `SNOWFLAKE_ACCOUNT` unset: fix the cause and run it again.

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
Have the user run it (it opens a browser for SSO); never ask for credentials in chat. That writes
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
Building a new dashboard?          → /start-app          (this skill's default mode)
Documenting an existing app?       → /start-app --spec <slug>
Porting an external Streamlit app? → /migrate-app
Just want to run one locally?      → /preview-app <slug>
```

This mode never fills in or reads credentials or keys, never edits existing CI or deploy config
(`init` only writes repo files that are missing), and installs nothing without a one-line
explanation and the user's yes.
