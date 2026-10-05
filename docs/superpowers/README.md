# Design docs (internal)

These are the working design documents behind larger StreamSnow changes: a
**spec** says what a feature should do and why, and a **plan** breaks it into
the PRs that build it. They are written for contributors and AI sessions doing
that work, not as user documentation, so they can describe phases that have not
shipped yet. For how StreamSnow behaves today, read the [docs index](../../README.md#documentation)
and the [CHANGELOG](../../CHANGELOG.md).

| Doc | What it covers |
|---|---|
| [specs/2026-10-04-sql-review-redesign.md](specs/2026-10-04-sql-review-redesign.md) | Page-based SQL review: one runnable file per page from `sql_review/index.yaml` |
| [plans/2026-10-05-sql-review-phase-1-layout-and-check.md](plans/2026-10-05-sql-review-phase-1-layout-and-check.md) | Phase 1 (shipped in 0.8.0): the file layout, `generate` and `check` |
| [plans/2026-10-05-sql-review-phase-2-live-review.md](plans/2026-10-05-sql-review-phase-2-live-review.md) | Phase 2: a live review against Snowflake, with a committed review log |
| [plans/2026-10-05-sql-review-phase-3-screen-comparison.md](plans/2026-10-05-sql-review-phase-3-screen-comparison.md) | Phase 3: comparing the numbers on screen with the SQL |
| [specs/2026-10-05-build-app-design.md](specs/2026-10-05-build-app-design.md) | What `/build-app` is for, how it is structured, and how it stays aligned with the principles |
