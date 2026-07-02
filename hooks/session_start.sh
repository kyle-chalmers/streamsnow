#!/usr/bin/env bash
# StreamSnow SessionStart hook — emits a one-line discovery pointer, and ONLY
# inside a StreamSnow repo (keeps token cost at zero everywhere else).
[ -f "${CLAUDE_PROJECT_DIR:-.}/streamsnow.config.yaml" ] || exit 0
echo "StreamSnow repo detected. Governance is in AGENTS.md. Skills: /start-app (front door: spec/scaffold/pages/setup/adopt) /preview-app /validate-app /review-app (--fix/--auto/--sql) /audit-lineage /feedback-app /ship-app /migrate-app. CLI: streamsnow doctor | configure | validate-app <slug> | preview <slug>."
