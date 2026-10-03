# Setup mode — first-time machine + repo configuration

Get a fresh machine and repo ready to build and preview apps. Interactive: propose each fix, run it
only after the user confirms, verify it before moving on. `streamsnow doctor` is the source of
truth — don't re-derive prerequisites by hand (no `which python`, no version greps).

## 0 · The CLI itself

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
`gh` is optional here and required later by `/ship-app`. `container-python` warns in a
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

| Tool | Why | macOS | Windows / Linux |
|---|---|---|---|
| Python 3.11+ (blocker) | runtime for apps + CLI | `brew install python@3.11` | `winget install Python.Python.3.11` / distro pkg or pyenv (hand to the user) |
| uv (blocker) | env + dependency manager | `brew install uv` | `irm https://astral.sh/uv/install.ps1 \| iex` / `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| git identity (blocker if unset) | commit attribution | `git config user.name/user.email` — prefer repo-local scope on multi-account machines | same |
| Snowflake CLI `snow` (optional) | `snow sql` diagnostics; the one connection store local preview reads | `uv tool install snowflake-cli` (a Homebrew `snow` can break on a newer system Python) | `uv tool install snowflake-cli` |
| GitHub CLI `gh` (optional; `/ship-app` needs it) | opens the PR and watches CI | `brew install gh`, then `gh auth login` | `winget install GitHub.cli` / distro pkg |
| pre-commit (required once configured) | runs the governance checks before each commit | `uv tool install pre-commit` | same |

After the repo is configured, `pre-commit install` wires the hooks. In git worktrees a repo-managed
`core.hooksPath` can make it refuse — confirm with the user before unsetting (it's sometimes
intentional).

## 2 · Repo configuration + governed repo files

First decide which case this is:

- The repo **already has Streamlit apps or its own agent commands or skills**: stop, that's
  [adopt mode](adopt.md), which maps onto what exists instead of scaffolding.
- Otherwise (an empty repo, or one with no `apps/` yet), the setup verb is
  `streamsnow init --no-starter-app`. It runs the config wizard (or reuses an existing
  `streamsnow.config.yaml`) and then writes the governed repo files: `AGENTS.md`, `CLAUDE.md`,
  `.gitignore`, `.pre-commit-config.yaml`, `.github/workflows/`, `README.md` and
  `deploy/tombstones.yml`. It writes no example app; `/start-app` scaffolds the real one next
  with `streamsnow new`. **Never run only `streamsnow configure` here**: `configure` writes the
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

### 2a · Find the user's Snowflake access

Every user arrives with a different setup: a `snow` connection from a past tutorial, a Snowflake
MCP server in their agent, a dbt profile, several accounts, or nothing at all. Work out which
before asking anything, and help them get to a working default `snow` connection, because local
preview reads it and `--connection` can take the account from it without anyone typing the
account into chat.

| What you find | What to do |
|---|---|
| A default `snow` connection (`snow-key-file` in `streamsnow doctor --format json` names it) | Use it. Confirm with the user that it is the account these apps are for. |
| `snow` connections, none of them default | List the names only: `snow connection list --format json \| python3 -c "import json,sys; [print(r.get('connection_name'), r.get('is_default')) for r in json.load(sys.stdin)]"`. Ask which one is this account. Making it the default (`snow connection set-default <name>`) repoints every tool that reads the default, so ask before running it. |
| No `snow` connection, but a Snowflake MCP server or a dbt profile | Investigate through those (2b). Then offer to add a `snow` connection as below, since local preview needs one. |
| No Snowflake access on this machine | Help set it up. Ask how they sign in: SSO (`--authenticator externalbrowser`), a key pair (`--private-key-file <path>` with `--authenticator SNOWFLAKE_JWT`), or a programmatic access token. Then have them run `snow connection add --connection-name <slug> --default` in their own terminal, adding only that sign-in flag; it prompts for the account identifier, user and the rest, so nothing is typed into chat. The account identifier is in Snowsight's account menu (bottom left) under the account details. Then have them run `snow connection test -c <slug>`, which may open a browser. |
| No Snowflake account | Point them to a Snowflake trial. Trial accounts have no compute pools, so expect the `warehouse` runtime. |

Never ask for a password, token or key in chat, never open or edit the connection files, and
never propose password-only auth (Snowflake's MFA rollout retires it). If the user would rather
not set up a connection now, carry on with whatever access exists and fall back to asking.

### 2b · Investigate (read-only)

Find the answers before asking anything. Use whatever Snowflake access this machine and this
agent session have, in this order, and say which source and role each finding came from:

1. **The `snow` CLI's default connection** (it reads `~/.snowflake/connections.toml` and
   `config.toml`). Its name is `detail.connection_name` of the `snow-key-file` check in
   `streamsnow doctor --format json` (present whenever a default connection exists). Run each probe
   as `snow sql -c <connection> --format json -q "<query>"`. This is the preferred source: it is
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
can show the account or a credential. Read named keys through a filter instead. The `->>` pipe
in each probe keeps only the columns you need, so owners and share origins stay off screen:

| Question | Probe | How to read it |
|---|---|---|
| Runtime | `SHOW COMPUTE POOLS ->> SELECT "name", "state" FROM $1` | Any pool listed: propose `container` (the default `SYSTEM_COMPUTE_POOL_CPU`, or the listed pool if that one is absent). None listed: propose `warehouse`, because trial accounts have no compute pools and the container runtime needs one; say that a paid account whose admin grants a pool can still choose `container`. |
| Account | none | With a `snow` connection, pass `--connection <name>`: the CLI reads the account and never prints it. With only an MCP server, `SELECT CURRENT_ORGANIZATION_NAME() \|\| '-' \|\| CURRENT_ACCOUNT_NAME()` returns a usable identifier, but it then appears on screen: ask before running it, or let the user type it. |
| Database | `SHOW DATABASES ->> SELECT "name", "kind", "comment" FROM $1` | Propose the curated or reporting database, never a raw or landing one. A comment saying what the data is for, or the user's own description of the app, outweighs the name; with neither, names like `ANALYTICS`, `REPORTING`, `MARTS` or `DW` point to curated data and `RAW`, `LANDING`, `INGEST`, `STAGING`, `SANDBOX` or `DEV` to raw. Ignore `SNOWFLAKE`, `SNOWFLAKE_LEARNING_DB`, `SNOWFLAKE_SAMPLE_DATA` and the StreamSnow app database (`STREAMSNOW_APPS` by default). Two plausible candidates: name both and ask. |
| Allowed schemas | `SHOW SCHEMAS IN DATABASE <db> ->> SELECT "name", "comment" FROM $1` | Propose the curated schemas (`MARTS`, `REPORTING`, `ANALYTICS`, `CURATED`, `GOLD`, `PRESENTATION`); skip `INFORMATION_SCHEMA`. When nothing matches those names but the database holds only one or two other schemas that are not raw, propose those and say the names gave no signal. |
| Denied schemas | same result | Propose the raw and staging schemas that actually exist (`RAW*`, `STG*`, `STAGING`, `LANDING`, `BRONZE`, `INGEST*`), not the `RAW,STAGING` default. When none exist, omit `--deny-schemas` so the default stands, and say it guards names that do not exist yet. An intermediate layer (`INT*`, `INTERMEDIATE`) goes in neither list unless the user says so. Never put a schema in both. |
| Deploy source | `SHOW GIT REPOSITORIES IN ACCOUNT ->> SELECT "database_name", "schema_name", "name" FROM $1` | Propose `stage-copy`. Offer `git-repository` only when a GIT REPOSITORY already exists, and say what it adds: an API integration (ACCOUNTADMIN creates it), a GitHub token stored as a Snowflake secret, and Snowflake needing network access to GitHub. |

These are SHOW and SELECT-over-SHOW only. **Never run DDL, grants, or anything that writes**, and
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
code. Then run, with the confirmed answers:

```
streamsnow init --no-starter-app --runtime container --connection <name> \
  --database ANALYTICS --schemas MARTS,REPORTING --deny-schemas RAW,STG_CRM \
  --deploy-source stage-copy
```

With all five answers passed, no prompt fires. `--connection` reads the account from that
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
  answer flags are its supported non-interactive path.
- **Not in Claude Code?** After `init`, `streamsnow agent-skills install --agent codex` copies these
  skills into the repo's `.agents/skills/`, where every teammate's Codex finds them; commit it.
- `streamsnow init` without the flag also scaffolds an `example-dashboard` starter app. That is
  the CLI-only path; in this skill the real app comes from `/start-app`, so pass the flag.
- The first deploy needs one-time Snowflake objects (database, schema, warehouse, roles, a CI
  service user, grants). `streamsnow deploy-setup --admin` prints the reviewable DDL; surface it
  for the user's Snowflake admin, never run it yourself.

## 3 · Connection (one store, owned by the user)

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

- **`streamsnow: command not found`** — not installed (step 0) or the tool bin dir isn't on PATH
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

This mode never fills in credentials, never edits existing CI or deploy config (`init` only
writes repo files that are missing), and installs nothing without confirmation.
