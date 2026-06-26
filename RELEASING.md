# Releasing StreamSnow

## Pre-publish privacy gate (do this before the repo goes public)

StreamSnow was extracted from a private monorepo, so before flipping the repo
public or cutting the first PyPI release:

1. **Automated scan** — must be clean:
   ```bash
   uv run python -m streamsnow.tools.check_export_clean .
   ```
   (Also runs as the `privacy-gate` CI job on every push.)
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

1. Bump `version` in `pyproject.toml` (and note changes in `CHANGELOG.md`).
2. Ensure `main` is green (lint-and-test, privacy-gate, wheel-smoke).
3. Tag and push:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
   The `publish` workflow builds the sdist + wheel and publishes to PyPI via OIDC.
4. Create a GitHub Release from the tag with the changelog notes.

## Flip the repo public (separate, deliberate step)

Only after the privacy gate passes and you've decided to open it:
```bash
GH_TOKEN="$GITHUB_TOKEN_PERSONAL" gh repo edit kyle-chalmers/streamsnow --visibility public --accept-visibility-change-consequences
```

## Claude Code plugin marketplace

Once public, users add the plugin with:
```
/plugin marketplace add kyle-chalmers/streamsnow
/plugin install streamsnow@streamsnow
```
No publish step is required for the plugin — it's served from the public repo.
