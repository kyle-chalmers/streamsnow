"""Validation and scaffolding tools.

Each tool is a small, CLI-invocable, structured-output program (``--format=md|json``,
exit codes ``0`` pass / ``1`` finding / ``2`` tool error). Skills, pre-commit, and
CI all shell out to these — they never embed prompt text or LLM reasoning.

Ported and genericized from the source monorepo's ``tools/`` in Phase 1+.
"""
