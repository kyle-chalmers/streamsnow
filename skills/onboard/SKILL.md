---
name: onboard
description: First-time machine setup for a StreamSnow repo — runs `streamsnow doctor`, walks the user through installing any missing prereqs (Python 3.11+, uv, git, Snowflake CLI, streamlit), then hands off to Snowflake configuration. Use when the user says "set me up", "onboard", "onboard me", "first time setup", "check my setup", or when prereq detection finds something missing during normal work.
---

# Onboard

Get a fresh machine ready to build and preview StreamSnow apps locally. This skill is interactive: propose fixes, run them only after the user confirms, and verify each fix before moving on. `streamsnow doctor` is the source of truth — do not re-derive prerequisites by hand.

## Prerequisites this covers

- **Python 3.11+** — runtime for apps and the CLI.
- **uv** — environment + dependency manager; apps install Streamlit and Snowpark per-app via uv.
- **git** — version control; commits should carry a real `user.name` / `user.email`.
- **Snowflake CLI (`snow`)** — optional locally, but needed for `snow sql` diagnostics and for piping the DDL that `streamsnow deploy-setup` emits. Deploys themselves run through CI, not from a laptop.
- **streamlit** — installed per-app by uv; `doctor` only flags it if your global setup expects it and it is absent.

## Steps

1. **Run the doctor.** `streamsnow doctor`. Read the per-check status line by line. Tell the user what was found in one line, e.g. "5 checks passed, 2 need attention."
2. **Walk each failing check one at a time.** For every FAIL or missing tool, state the platform-appropriate fix from the table below, ask the user to confirm, then run it. Do not batch installs — one fix, one re-check. If the user declines, mark it skipped and continue.
3. **Re-run the single check after each fix** (`streamsnow doctor` again, or just inspect the relevant line) to confirm it flipped to green before moving on. Surface anything that stays red with the exact remediation rather than papering over it.
4. **(Optional) Install local commit gating.** If the repo ships pre-commit hooks, `pre-commit install` wires the governance checks (the `streamsnow check` subcommands `schema-refs`, `security`, `caching`, `bind-predicates`) to run before each commit. In git worktrees a repo-managed `core.hooksPath` can make `pre-commit install` refuse — if so, confirm with the user before unsetting it, since it is sometimes set intentionally.
5. **Hand off to Snowflake configuration.** Tell the user to run `streamsnow configure`, which writes `streamsnow.config.yaml` (account, role, warehouse, governance schema allow/deny). Do not author that file by hand — `configure` is idempotent and re-runnable, so it is the right tool for both first setup and later edits. On a brand-new repo, `streamsnow init` runs `configure` plus a starter-app scaffold in one shot.
6. **Point at the next step.** Once the environment is green and config exists, branch to the right builder skill (see "Next step" below).

## Fix recipes by tool

Pick the row for the user's OS. Always confirm before running.

**Python 3.11+** (blocker):

| OS | Command |
|---|---|
| macOS | `brew install python@3.11` (or pyenv) |
| Windows | `winget install Python.Python.3.11` |
| Linux | distro package manager (`apt install python3.11`, `dnf install python3.11`) or `pyenv install 3.11` — hand to the user, don't run it for them |

**uv** (blocker):

| OS | Command |
|---|---|
| macOS | `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh` |
| Windows | `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` |
| Linux | `curl -LsSf https://astral.sh/uv/install.sh | sh` |

**git identity** (blocker if unset): ask the user for their name and email, then set it — prefer **repo-local** scope so a multi-account machine doesn't get the wrong attribution:

```bash
git config user.name "<name>"
git config user.email "<email>"
```

**Snowflake CLI (`snow`)** (optional): the repo deploys through CI; `snow` is for `snow sql` diagnostics and for applying the one-time DDL from `streamsnow deploy-setup`.

| OS | Command |
|---|---|
| macOS | `brew install snowflake-cli` |
| Windows / Linux | `uv tool install snowflake-cli` (or `pipx install snowflake-cli-labs`) |

## Container vs. warehouse runtime

You do not choose a runtime here — it lives in `streamsnow.config.yaml` and is set during `streamsnow configure`. But know the distinction so you can answer the user:

- **Warehouse runtime** is the default and needs nothing extra locally — `streamsnow preview` runs the app against a Snowflake virtual warehouse defined in `snowflake.objects` with the role from `snowflake.roles`.
- **Container runtime** (Snowpark Container Services) only matters at deploy time and is also config-driven. It does **not** change local onboarding: the same Python/uv/git prereqs apply. If the user asks, point them at `streamsnow configure` to set or change the runtime, not at any manual edit.

## Gotchas and edge cases

- **Don't hand-edit `streamsnow.config.yaml`.** Re-run `streamsnow configure`; it prefills from the current file so re-running is an edit, not a restart. It writes no secrets.
- **Local secrets are separate from config.** `streamsnow preview` reads `.streamlit/secrets.toml`, which is per-app and gitignored. Never ask the user to paste credentials into chat — redirect them to the file. Common trap: the `account` field is a locator (e.g. `ab12345.us-west-2`), not the full `*.snowflakecomputing.com` hostname — including the suffix double-appends and fails auth.
- **Local role should match the deployed viewer role**, not a broad personal/BI role, so grant gaps surface locally before they ship. The scaffolded `secrets.toml.example` already encodes the right role — keep it.
- **uv, not bare pip.** Apps are run with `uv run` from the repo so the per-app environment is used; activating a stray global venv is a common source of "works for me" drift.
- **Worktrees and hooks.** A set `core.hooksPath` (common in worktrees) blocks `pre-commit install`. Confirm before unsetting.

## Troubleshooting

- **`streamsnow: command not found`** — the CLI isn't on PATH. Confirm how it was installed (`uv tool install`, pipx, or editable) and that the install bin dir is on PATH; re-open the shell.
- **`doctor` keeps reporting Python too old** — a newer Python may be installed but not first on PATH. Check `python3 --version` and which interpreter resolves; install via the table above or adjust PATH, then re-run.
- **A check flips back to red after a fix** — re-run that single check and read its remediation verbatim. Don't advance past a red blocker; the downstream builder skills assume a green environment.
- **`streamsnow preview` fails to connect** — almost always `secrets.toml`: wrong `account` format (see gotchas), wrong role, or a missing warehouse grant. Print the connection error verbatim and have the user recheck the file, not chat.
- **Port already in use on preview** — `streamsnow preview <slug> --port 8502` to pick a free port.

## Done when

- `streamsnow doctor` reports every check passing (or the only remaining items are optional and the user has accepted skipping them).
- The user knows that `streamsnow configure` (or `streamsnow init` on a fresh repo) writes `streamsnow.config.yaml` next, and that secrets live in a per-app `.streamlit/secrets.toml`.
- You've printed the "Next step" branch and let the user choose — don't pick for them.

## Next step

```
Fresh repo, no config yet?
  → streamsnow init   (configure + starter-app scaffold in one)

Already configured, building a new dashboard?
  → /new-app          (interactive scaffold)
  or /start-app       (guided idea-to-app kickoff)

Porting an existing Streamlit app into this repo?
  → /migrate-app

App already in apps/? Run it locally:
  → /preview-app <slug>
```

Typical chain: `/onboard` → (`/new-app` | `/start-app` | `/migrate-app`) → `/preview-app` → `/validate-app` → `/review-app` → `/apply-review` → `/ship-app`.

## What this skill does NOT do

- Does not install anything without confirmation, and never batches installs.
- Does not author `streamsnow.config.yaml` — `streamsnow configure` owns it.
- Does not fill in Snowflake credentials — the user owns `secrets.toml`.
- Does not touch CI, GitHub auth, or deploy configuration — onboarding is local-only.
