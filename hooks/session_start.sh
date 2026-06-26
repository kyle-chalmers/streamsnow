#!/usr/bin/env bash
# StreamSnow SessionStart hook — emits a one-line discovery pointer, and ONLY
# inside a StreamSnow repo (keeps token cost at zero everywhere else).
[ -f "${CLAUDE_PROJECT_DIR:-.}/streamsnow.config.yaml" ] || exit 0
echo "StreamSnow repo detected. Governance is in AGENTS.md. Skills: /onboard /refine-requirements /new-app /add-page /preview-app /validate-app /ship-app /start-app. CLI: streamsnow doctor | configure | validate-app <slug> | preview <slug>."
