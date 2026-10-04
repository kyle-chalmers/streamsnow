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
  that prompt, so give them the command; prefer installs that need no password (uv, nvm);
- creating the CI key pair and setting the private-key secret (§2b): never generate, read, print,
  or pipe a private key yourself.

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
`ci-secrets` belongs to §2b. `gh` is optional here and required later by `/ship-app`. `container-python` warns in a
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
  sure `pre-commit install` ran (§1), then go to §2b and §3. When it lists missing files, run
  `streamsnow init --no-starter-app`; it reuses the config and writes only the missing files.
  Never treat the config alone as "already set up".
- The repo **already has Streamlit apps or its own agent commands or skills** but no config: stop, that's
  [adopt mode](adopt.md), which maps onto what exists instead of scaffolding.
- Otherwise (an empty repo, or one with no `apps/` yet), run:

  ```
  streamsnow init --no-starter-app
  ```

  This is the setup verb. It runs the config wizard (or reuses an existing
  `streamsnow.config.yaml`) and then writes the governed repo files: `AGENTS.md`, `CLAUDE.md`,
  `.gitignore`, `.pre-commit-config.yaml`, `.github/workflows/`, `README.md` and
  `deploy/tombstones.yml`. It writes no example app; `/start-app` scaffolds the real one next
  with `streamsnow new`. **Never run only `streamsnow configure` here**: `configure` writes the
  config file and nothing else, and `streamsnow new` writes app files only, so a repo set up that
  way has no hooks, no CI and no `.gitignore` (an app's `.streamlit/secrets.toml` could then be
  committed). `streamsnow new` warns when those files are missing; the fix is the same command.

The wizard detects what it can and asks **at most 5 questions**: runtime, Snowflake account, the
database apps query, the allowed schemas, and the deploy source. Everything else (project name,
roles, warehouse, schema names, container objects) is written as a sensible default with an inline
comment saying when to change it; the file is the editing surface. To change answers later, run
`streamsnow configure` (it prefills from the current file, so re-running is an edit, not a
restart); existing repo files are left alone by a re-run of `init --no-starter-app`.

- **Don't hand-author `streamsnow.config.yaml` from scratch**: the wizard owns its shape.
- **Not in Claude Code?** After `init`, `streamsnow agent-skills install --agent codex` copies these
  skills into the repo's `.agents/skills/`, where every teammate's Codex finds them; commit it.
- `streamsnow init` without the flag also scaffolds an `example-dashboard` starter app. That is
  the CLI-only path; in this skill the real app comes from `/start-app`, so pass the flag.
- The first deploy needs one-time Snowflake objects (database, schema, warehouse, roles, a CI
  service user, grants). `streamsnow deploy-setup --admin` prints the reviewable DDL; surface it
  for the user's Snowflake admin, never run it yourself.

## 2b · Shared repo settings (once per repo; a teammate usually finds them done)

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
- The key pair is created by the user or their admin with the commands in
  [docs/deploy-setup.md](../../docs/deploy-setup.md) §2, never by you: the public half goes into
  the admin script, the private half only into GitHub.
- Set these yourself after confirming each value with the user, with
  `gh secret set NAME --body "<value>"`: `SNOWFLAKE_ROLE` (`roles.ci_role`),
  `SNOWFLAKE_WAREHOUSE` (a warehouse the CI role can use, usually `objects.default_warehouse`),
  and `SNOWFLAKE_USER` (the `CREATE USER` name in the admin script; the admin may have renamed it).
- The private key is theirs to set. Give the command and the reason in one line: "This one is a
  password-like key, so you run it and I never see it":
  `gh secret set SNOWFLAKE_PRIVATE_KEY_RAW < path/to/ci_key.p8` (plus
  `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` only if the key is encrypted).
- Re-run doctor. Only when `ci-secrets` lists nothing but `SNOWFLAKE_ACCOUNT`, set it
  (`snowflake.account`) and tell the user the next merge to `main` will deploy.

## 3 · Connection (one store, owned by the user)

**Check before adding anything.** When the machine already has a default `snow` connection (a
prior tutorial, another project), the wizard writes that name into `snowflake.connection_name`,
and `init`'s `Next:` block says there is nothing to add. If `streamsnow doctor --format json`
already reads `snow-connection: ok: true`, skip to the key-file check below: **do not** have the
user run `snow connection add … --default`, which would add a second connection and repoint the
default every other tool reads. If the check fails but its hint names an existing default
connection, the usual fix is to set `snowflake.connection_name` to that name (confirm with the
user first) rather than create a new one.

Only when there is no usable connection: `streamsnow init` (and `configure`) print the exact
`snow connection add … --default` command for the account.
Have the user run it (it opens a browser for SSO) — never ask for credentials in chat. That writes
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
