#!/usr/bin/env bash
# StreamSnow SessionStart hook: one line of orientation, and ONLY where it
# helps: a StreamSnow repo (config present, with a nudge when this clone has no
# pre-commit hook), or a repo that has Streamlit apps or the plugin enabled but
# no config yet (the /onboard nudge). Silent everywhere else, so the token cost
# is zero in unrelated repos. Fail-open: nothing here may block a session.
root="${CLAUDE_PROJECT_DIR:-.}"
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)}"
ver="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$plugin_root/.claude-plugin/plugin.json" 2>/dev/null | head -1)"
cli=""
command -v streamsnow >/dev/null 2>&1 || cli=" CLI not on PATH: uv tool install streamsnow."

if [ ! -f "$root/streamsnow.config.yaml" ]; then
  # Streamlit apps without StreamSnow config: point at /onboard (adopt), once.
  if ls "$root"/apps/*/streamlit_app.py >/dev/null 2>&1; then
    echo "StreamSnow ${ver:-?}: this repo has apps/*/streamlit_app.py but no streamsnow.config.yaml: run /onboard (it maps onto existing apps and never scaffolds over them).${cli}"
    exit 0
  fi
  # The plugin is enabled for this repo, but the repo isn't set up yet.
  if grep -q '"streamsnow@streamsnow"[[:space:]]*:[[:space:]]*true' "$root/.claude/settings.json" 2>/dev/null; then
    echo "StreamSnow ${ver:-?}: StreamSnow is enabled here but the repo isn't set up yet: run /onboard.${cli}"
  fi
  exit 0
fi

# This clone has no pre-commit hook (the same path doctor's pre-commit-hook row checks).
clone=""
if command -v git >/dev/null 2>&1; then
  hook="$(git -C "$root" rev-parse --path-format=absolute --git-path hooks/pre-commit 2>/dev/null)"
  if [ -n "$hook" ] && [ ! -f "$hook" ]; then
    clone=" This clone isn't set up yet (no pre-commit hook): run /onboard."
  fi
fi

echo "StreamSnow ${ver:-?} repo. Governance: AGENTS.md. Skills: /onboard (setup) /build-app (spec/build; runs preview, validate and review itself) /preview-app /validate-app /review-app /sql-review /ship-app /migrate-app. Deploy-safety guard is ACTIVE (destructive snow/SQL commands pause; /ship-app is the deploy path). Key guard is ACTIVE (Claude's tools can't open ~/.streamsnow-ci). Review gate is ACTIVE (warn-only; REVIEW_GATE_OFF=1 or apps/<slug>/.review/SKIP silences it).${clone}${cli}"
