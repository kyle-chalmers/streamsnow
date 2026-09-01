# Setup mode — first-time machine + repo configuration

Get a fresh machine and repo ready to build and preview apps. Interactive: propose each fix, run it
only after the user confirms, verify it before moving on. `streamsnow doctor` is the source of
truth — don't re-derive prerequisites by hand (no `which python`, no version greps).

## 1 · Machine prerequisites

Run `streamsnow doctor --format json` and read the per-check results — each check is one object:

```json
{"name": "uv", "ok": false, "level": "required", "detail": {...}, "hint": "install uv — ..."}
```

Report in one line ("5 checks passed, 2 need attention"), then walk each `ok: false` check one at a
time — propose the fix (start from the check's own `hint`; the table below gives per-OS commands),
run it on confirmation, then re-run `streamsnow doctor --format json` and confirm that check now
reads `ok: true` before moving on. Never batch installs. `level` decides severity: a `required`
failure blocks the build phases (doctor exits 1); an `optional` one (`snow`, `streamlit`) is
offered, skippable. The config check flips level by context — `optional` when no
`streamsnow.config.yaml` exists yet, `required` when one exists but fails validation, so a
malformed config is never masked as "not configured". If the user declines a fix, mark it skipped
and continue. Exit codes: 0 = all required checks pass, 1 = a required check failed, 2 = the
doctor itself failed (report the error verbatim).

| Tool | Why | macOS | Windows / Linux |
|---|---|---|---|
| Python 3.11+ (blocker) | runtime for apps + CLI | `brew install python@3.11` | `winget install Python.Python.3.11` / distro pkg or pyenv (hand to the user) |
| uv (blocker) | env + dependency manager | `brew install uv` | `irm https://astral.sh/uv/install.ps1 \| iex` / `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| git identity (blocker if unset) | commit attribution | `git config user.name/user.email` — prefer repo-local scope on multi-account machines | same |
| Snowflake CLI `snow` (optional) | `snow sql` diagnostics; piping `deploy-setup` DDL | `brew install snowflake-cli` | `uv tool install snowflake-cli` |

Optionally `pre-commit install` to run the governance checks before each commit. In git worktrees a
repo-managed `core.hooksPath` can make it refuse — confirm with the user before unsetting (it's
sometimes intentional).

## 2 · Repo configuration

Run `streamsnow configure`. It detects what it can and asks **at most 5 questions** — runtime,
Snowflake account, the database apps query, the allowed schemas, and the deploy source. Everything
else (project name, roles, warehouse, schema names, container objects) is written as a sensible
default with an inline comment saying when to change it; the file is the editing surface, and
re-running `configure` prefills from it, so re-running is an edit, not a restart.

- **Don't hand-author `streamsnow.config.yaml` from scratch** — `configure` owns its shape.
- On a brand-new repo, `streamsnow init` runs configure plus a starter-app scaffold in one shot.
- If the repo **already has Streamlit apps or its own Claude commands**, stop — that's
  [adopt mode](adopt.md), which maps onto what exists instead of scaffolding.

## 3 · Secrets (separate from config, owned by the user)

Local preview reads `apps/<slug>/.streamlit/secrets.toml` (gitignored). Never ask the user to paste
credentials into chat — point them at the file. Two classic traps:

- `account` is a locator (`ab12345.us-west-2`), **not** the full `*.snowflakecomputing.com`
  hostname — the connector appends the suffix, and a doubled one fails auth.
- Set `role` to the deployed **viewer role** (from config), not a broad personal role — a wide role
  hides missing grants locally that then ship as empty dashboards.

## Troubleshooting

- **`streamsnow: command not found`** — the install bin dir isn't on PATH; re-open the shell.
- **doctor keeps reporting Python too old** — a newer Python exists but isn't first on PATH.
- **A check flips back to red** — read its `hint` and `detail` verbatim; don't advance past a
  `required` failure.
- **Preview can't connect** — almost always `secrets.toml` (account format, role, warehouse grant).
  Print the connection error verbatim and have the user recheck the file.

## Done → next step

Everything green and config written. Branch on intent, and let the user choose:

```
Building a new dashboard?          → /start-app          (this skill's default mode)
Documenting an existing app?       → /start-app --spec <slug>
Porting an external Streamlit app? → /migrate-app
Just want to run one locally?      → /preview-app <slug>
```

This mode never fills in credentials, never touches CI or deploy config, and installs nothing
without confirmation.
