#!/usr/bin/env bash
# StreamSnow SessionStart hook — one line of orientation, and ONLY where it
# helps: a StreamSnow repo (config present), or a repo that has Streamlit apps
# but no config yet (the adopt nudge). Silent everywhere else, so the token cost
# is zero in unrelated repos. Fail-open: nothing here may block a session.
root="${CLAUDE_PROJECT_DIR:-.}"
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)}"
ver="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$plugin_root/.claude-plugin/plugin.json" 2>/dev/null | head -1)"
cli=""
command -v streamsnow >/dev/null 2>&1 || cli=" CLI not on PATH: uv tool install streamsnow."

if [ ! -f "$root/streamsnow.config.yaml" ]; then
  # Streamlit apps without StreamSnow config → point at adopt mode, once.
  ls "$root"/apps/*/streamlit_app.py >/dev/null 2>&1 || exit 0
  echo "StreamSnow ${ver:-?}: this repo has apps/*/streamlit_app.py but no streamsnow.config.yaml — run /start-app --setup (adopt mode maps onto what exists; it never scaffolds over it).${cli}"
  exit 0
fi

echo "StreamSnow ${ver:-?} repo. Governance: AGENTS.md. Skills: /start-app (front door: setup/adopt/spec/build; runs preview, validate and review itself) /preview-app /validate-app /review-app /audit-lineage /feedback-app /ship-app /migrate-app. Deploy-safety guard is ACTIVE (destructive snow/SQL commands pause; /ship-app is the deploy path). Review gate is ACTIVE (warn-only; REVIEW_GATE_OFF=1 or apps/<slug>/.review/SKIP silences it).${cli}"
