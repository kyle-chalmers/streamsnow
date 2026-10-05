# Contributing to StreamSnow

Thanks for your interest! StreamSnow is in beta and under active development.

## Mission first

Every change is judged against the mission and vision at the top of the
[README](README.md) and the nine [principles](docs/principles.md). Two of them are worth restating here because
they decide most reviews: **faithful to a real fleet** (a check that fails a
well-run production app is a defect in the check until proven otherwise —
`tests/fixtures/fleet/` is the regression net, extend it when you find a new
shape) and **leaving should be cheap** (everything StreamSnow writes into a repo
stays a plain file the repo owns).

## Maintainers

One maintainer today. Co-maintainers are welcome: open an issue describing the
area you want to own (a check, the scaffolder, a skill), land two reviewed PRs
there, and ask for write access. Releases follow [RELEASING.md](RELEASING.md).

## Ground rules

- **One implementation, many consumers.** Validation/scaffolding logic lives in
  the `streamsnow` Python package. The CLI, the Claude Code plugin, pre-commit,
  and CI all call that same code — never fork logic into a skill or a workflow.
- **Tools are CLI-first and structured.** Each **governance check** tool is a
  small program with `--format=md|json` output and meaningful exit codes
  (`0` pass, `1` finding, `2` tool error). Other verbs use the interface that
  fits their domain — `preview` speaks `--json`, the deploy verbs emit SQL.
  Skills shell out to all of them; they never embed prompt text.
- **A tool's docstring carries its rationale.** Not what the code does — why
  the check exists and the concrete failure it prevents (ideally the incident
  shape that motivated it, genericized). A future maintainer deciding whether
  to relax a rule needs the why, and the docstring is the only place it
  survives refactors.
- **Every tool ships with fixture tests.** Behavior is pinned with `tmp_path`
  fixture apps in the fictional Acme domain — no network, no reliance on the
  developer's machine, and never fixture content copied from a private repo.
- **No org-specific values in committed code.** Anything Snowflake-, company-,
  or brand-specific belongs in `streamsnow.config.yaml`, not hardcoded.
- **Secrets never go in the repo.** Not in config, not in tests, not in docs.

## Dev setup

```bash
git clone https://github.com/kyle-chalmers/streamsnow.git
cd streamsnow
uv sync --extra dev
```

## Before you open a PR

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -q -rs
uv run python -m streamsnow.tools.check_export_clean .
```

CI runs the same checks on Linux, macOS and Windows. Keep changes focused; describe what
tool/skill/template you touched and why, and fill in the PR template.

- Add one line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for every
  user-visible change.
- Adding a command, flag or argument? Regenerate the CLI snapshot with
  `uv run python tests/test_cli_surface.py --update` and say so in the PR. Renaming or
  removing one is a breaking change ([Versioning and stability](docs/versioning.md)).

## Where feedback goes

- **Questions** and **open-ended ideas** go to
  [Discussions](https://github.com/kyle-chalmers/streamsnow/discussions) (Q&A, Ideas).
  Ideas that gain support and fit the mission become issues.
- **Bugs** and **well-scoped feature requests** go to
  [issues](https://github.com/kyle-chalmers/streamsnow/issues/new/choose), through the forms.
  Blank issues are off so every report arrives with a version, an install method and steps.
- **Vulnerabilities** go privately through [SECURITY.md](SECURITY.md), never a public issue.

See [SUPPORT.md](SUPPORT.md) for what to expect and how fast.

## How issues move

StreamSnow is maintained by one person with AI agents doing the routine work. The
maintainer makes two decisions per change, what gets built and what gets merged, and
automation does the rest:

1. **Triage (automatic).** `claude-triage` labels every new issue with a type, an area, a
   priority and one status. The model only classifies: it cannot comment or edit. A script
   applies its labels and posts fixed, pre-written comments.
   - `status:needs-info`: details are missing; the comment lists them. Reply or edit the
     issue and it is re-checked. Issues still waiting after 14 quiet days close (reopen anytime).
   - `status:needs-maintainer`: may be out of scope, or changes existing behavior; the
     maintainer decides.
   - `status:triaged`: complete and in scope.
   - Possible duplicates are labeled and linked, never closed automatically.
2. **Approval (maintainer).** The maintainer labels a triaged issue `ready-for-agent`, asks
   a question, or closes it with a reason. Only people with triage access can apply labels,
   so this label is the gate.
3. **Implementation (agent).** `claude-implement` picks up `ready-for-agent` issues
   (`status:agent-working`), writes the change and its tests following [.claude/CLAUDE.md](.claude/CLAUDE.md),
   runs every check, and opens a PR that closes the issue. If the issue is ambiguous or
   needs a breaking change, it stops and asks on the issue instead.
4. **Review and merge (maintainer).** CI and the Claude Code Review workflow run on the PR. The maintainer
   asks for changes with `@claude ...` comments, or merges.

Want to implement something yourself? Comment on a `status:triaged` issue (or one labeled
`help wanted` / `good first issue`) so no agent picks it up while you work on it.

## What gets accepted

- Changes that serve the mission and respect the nine [principles](docs/principles.md). The
  "This is for you if / not for you" list in the [README](README.md) is the scope line.
- **Small and focused first.** Bug fixes and docs fixes are welcome directly as PRs. A new
  feature or a new check needs an issue that the maintainer marked `status:triaged` or
  `ready-for-agent` first, so nobody spends a weekend on something that will not merge.
- A change that breaks the stable surface (commands, flags, config keys, check results,
  skill names, generated files) needs the maintainer's sign-off and a CHANGELOG entry that
  says what to do. From 1.0.0 it also follows the deprecation policy in
  [docs/versioning.md](docs/versioning.md).

## AI-assisted contributions

AI tools are welcome here (this project is built with them), on these terms, adapted from
policies like Ghostty's:

- **Disclose it.** Say which tool you used and for what, in the PR template.
- **Own it.** You have read every line, you can explain it, and you ran the checks. "The
  model wrote it" is not an answer to a review question.
- **Issue first for features.** AI-generated feature PRs without an accepted issue are
  closed, because an issue is the spec, and the maintainer can run an agent on a spec too.
- **No unreviewed bulk output.** Mass-generated PRs, issues or vulnerability reports that
  were not checked by a person are closed without discussion, and repeat offenders blocked.

## Reporting issues

Use the [issue forms](https://github.com/kyle-chalmers/streamsnow/issues/new/choose): they
ask for the StreamSnow version (`streamsnow --version`), how it is installed, your
runtime/deploy-source config (redact secrets) and steps to reproduce. Report a security
vulnerability privately instead, as [SECURITY.md](SECURITY.md) describes. Everyone taking
part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md).
