# Releasing StreamSnow

## Privacy gate (before every release)

StreamSnow was extracted from a private monorepo. It has been public and on PyPI
since 0.7, and every release still passes this gate before it is tagged:

1. **Automated scan** (must be clean):
   ```bash
   uv run python -m streamsnow.tools.check_export_clean .
   ```
   The committed scanner only knows generic leaks (personal paths, private
   keys, tokens, real email addresses). Names specific to where you work
   (employer, internal hosts, table names, ticket prefixes) go in
   `.streamsnow/export-denylist.txt`, one term per line (`re:` prefix for a
   regex, `#` for comments). That file is gitignored on purpose: a committed
   list of names not to leak is itself the leak. The `privacy-gate` CI job runs
   the generic checks on every push; run the scan locally, with your denylist
   present, before every release.
2. **Human review** — skim for anything the scanner can't know is sensitive:
   - real company/people/customer names, internal URLs, ticket IDs, account locators
   - screenshots or example data derived from real systems
   - anything in `git log` history (the scan only sees the working tree)
3. Confirm `LICENSE`, `README`, and `CONTRIBUTING` say what you intend.

## One-time PyPI setup (Trusted Publishing — no stored token)

1. Create the `streamsnow` project on PyPI (or reserve the name).
2. PyPI → project → **Publishing** → add a **Trusted Publisher**:
   - Owner: `kyle-chalmers` · Repo: `streamsnow` · Workflow: `publish.yml` · Environment: `pypi`
3. In GitHub repo settings, create an environment named `pypi`.

## Cut a release

1. Bump the version in **lockstep across four files**:
   - `pyproject.toml`
   - `.claude-plugin/plugin.json`
   - `streamsnow/__init__.py` (the `__version__` fallback)
   - `uv.lock` (run `uv lock`, then `uv lock --check` to confirm)

   A plugin-only install reads `plugin.json`, a pip install reads the wheel
   metadata, and `streamsnow --version` reads `__init__.py`, so a partial bump
   makes them disagree about what is installed.
   Then check the Playwright CLI pin in `skills/_shared/playwright-walkthrough.md`
   (`@playwright/cli@X.Y.Z`, the only place it is written): compare it with
   `npm view @playwright/cli version`. When bumping it, read
   `npm view @playwright/cli@<new> dependencies` for the `playwright-core` it pins, check
   that core's Node floor (`npm view playwright-core@<that version> engines`) against
   `_NODE_MIN_MAJOR` in `streamsnow/tools/doctor.py`, and run one UI walkthrough
   (`/preview-app` on the sample app) on the new version before tagging. Each CLI release
   tracks a Playwright alpha, so expect breakage on bumps, and each bump downloads a new
   browser (about 150 MB). It is an exact pin on purpose: `@latest` would let an upstream
   release change the walk under a plugin version you already shipped.
2. Close the changelog: in `CHANGELOG.md`, move every entry under `## [Unreleased]` into a
   new `## [X.Y.Z] - YYYY-MM-DD` heading directly below it, and leave an empty
   `## [Unreleased]` above it for the next change.
3. Ensure `main` is green (lint-and-test, privacy-gate, wheel-smoke).
4. Tag and push. Use the fully-qualified refspec:
   ```bash
   git tag vX.Y.Z
   git push origin refs/tags/vX.Y.Z
   ```
   The `publish` workflow builds the sdist + wheel and publishes to PyPI via OIDC.

   `git push origin vX.Y.Z` is ambiguous and will fail if a release BRANCH of
   the same name exists, which is the convention here (`v0.6.1`, `v0.6.2` are
   branches as well as tags). Git refuses with "matches more than one" rather
   than guessing, so it is a stop, not a mis-push - but it stops you mid-release.
   `refs/tags/` names the tag unambiguously; `refs/heads/vX.Y.Z` pushes the branch.
5. Create a GitHub Release from the tag with the `## [X.Y.Z]` changelog notes.

## Flip the repo public (separate, deliberate step)

Only after the privacy gate passes and you've decided to open it:
```bash
GH_TOKEN="$GH_TOKEN" gh repo edit kyle-chalmers/streamsnow --visibility public --accept-visibility-change-consequences
```

## Claude Code plugin marketplace

Once public, users add the plugin at project scope, from their apps repo:
```bash
claude plugin marketplace add --scope project kyle-chalmers/streamsnow
claude plugin install --scope project streamsnow@streamsnow
```
No publish step is required for the plugin — it's served from the public repo.

## Pre-release: official docs link sweep

The link registry test is offline. Before tagging, run the online sweep once so
a Snowflake docs reorganization does not ship as dead links:

```bash
uv run python scripts/check_docs_links.py --online
```

Every URL must answer 200 without redirecting. A 308 means the page moved:
update `docs/snowflake-docs.md` (and any inline link) to the new canonical path.

## Path to 1.0.0

1.0.0 turns the [stability promise](docs/versioning.md) into a firm commitment, so it ships
only when every gate below is true. Track the work in the `1.0.0` milestone.

**Before the 1.0 release candidate:**

- Every planned breaking change has landed. Nothing on the stable surface is still
  expected to move, because from 1.0.0 the deprecation policy applies to every rename or
  removal.
- Native Windows is finished or explicitly documented as out of scope: the session hook
  (`hooks/session_start.sh`) runs without bash or WSL, and no Windows test is skipped
  without a written reason.
- `tests/fixtures/cli_surface.json` matches the surface you intend to support for 1.x.

**Release gates for 1.0.0 (all on the release commit):**

1. Every CI job is green, including the Windows rows of `lint-and-test`.
2. The privacy scan passes locally **with** your `.streamsnow/export-denylist.txt` present.
3. `uv run python scripts/check_docs_links.py --online` passes.
4. One end-to-end UI walkthrough (`/preview-app` on the sample app) on the pinned
   Playwright CLI version.
5. One live onboarding run in a scratch repo, by the maintainer, covering both admin paths
   and both connection paths.
6. `CHANGELOG.md` says semantic versioning applies from 1.0.0, and the release notes link
   [docs/versioning.md](docs/versioning.md).

