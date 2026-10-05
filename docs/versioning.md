# Versioning and stability

StreamSnow uses [semantic versioning](https://semver.org/). This page says what that promise
covers, how things are retired, and what has to be true before 1.0.0.

**Before 1.0.0 (today, 0.x):** the surface below is already treated as stable in spirit, but
the deprecation policy does not apply yet. A breaking change can land in any minor release
(0.7 → 0.8) without a deprecation period, always with a CHANGELOG entry that says what changed
and what to do. Until then StreamSnow also assumes it has no users: a change to generated files
ships without migration code or a refresh step for apps an older release generated.

**From 1.0.0:** anything on the stable surface changes incompatibly only in a major release
(1.x → 2.0), after the deprecation window below.

## The stable surface

These are what your repo, your CI and your habits depend on, so they are covered:

| Surface | Examples | How it is enforced |
|---|---|---|
| CLI commands, sub-commands, flags and positional arguments | `streamsnow init --dir`, `streamsnow check schema-refs --format json` | `tests/test_cli_surface.py` snapshot |
| Governance check results | exit codes `0` pass / `1` finding / `2` tool error; the keys of `--format=json` output | per-check tests |
| Config schema | keys of `streamsnow.config.yaml`, `schema_version` | `streamsnow/config.py` validation |
| Plugin skills | skill names (`/build-app`, `/ship-app`, ...) and their arguments | `tests/test_plugin_surface.py` pins the skill names and checks that each skill declares an `argument-hint`; the arguments themselves are not tested |
| Hook behavior | what the deploy guard blocks, what the key guard denies, when the review gate nudges | hook tests |
| Generated repo files | CI and deploy workflows, pre-commit config, `AGENTS.md`, `CLAUDE.md` written by `streamsnow init` and refreshed by `streamsnow update` | template tests |

**Not covered:** internal Python modules and functions (import `streamsnow.*` at your own
risk), the wording of skill prompts, the human-readable `md` output of checks, and anything
marked experimental in its help text.

## Stricter checks count as breaking

StreamSnow is a governance tool: a new rule, or a tightened one, can turn a green CI run red
the morning after an upgrade. So a check change that can fail a repo that passed before:

1. ships **warn-only** in its first minor release (reported, exit code unchanged), with a
   CHANGELOG entry and a config switch to opt in to enforcement early;
2. becomes **enforcing** in the next minor release.

A fix that makes a check *accept* more well-run code (README principle 7, "faithful to a real
fleet") is not breaking and ships immediately.

## Deprecation policy

**Applies to 1.0.0 and later.** Before 1.0.0, a rename or removal can ship in any minor
release; a warning or redirect stub is welcome but not required.

From 1.0.0, when something on the stable surface is renamed or removed:

1. **Deprecate in a minor release.** The old name keeps working, prints a warning on stderr
   that names the replacement, and gets a `### Deprecated` CHANGELOG entry. A retired skill
   becomes a redirect stub that points at its replacement.
2. **Keep it working** for at least one further minor release **and** at least 90 days.
3. **Remove it in the next major release.** The removal gets a `### Removed` entry.

## Config schema changes

`streamsnow.config.yaml` carries `schema_version`. An older StreamSnow already refuses a newer
config instead of misreading it. No migration code exists yet, because no config change has
needed one. When one does, the breaking config change will bump `schema_version` and ship a
migration in `streamsnow update` that rewrites the previous version's file.

## Support

- Fixes ship in the latest release only (see [SECURITY.md](../SECURITY.md)).
- Python support follows CPython's end-of-life schedule. Dropping a Python version is a
  minor release, announced one release ahead.
- Raising the minimum Claude Code version the plugin needs is a minor release.

## Marking breaking changes

- CHANGELOG: a `### Breaking` heading in the release, above everything else.
- Commits: `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer.
- PRs: the `breaking-change` label, applied by the maintainer.

## Path to 1.0.0

1.0.0 ships once the planned breaking changes have landed, the stable surface above is frozen
in its tests, and the release gates in [RELEASING.md](../RELEASING.md#path-to-100) pass.
