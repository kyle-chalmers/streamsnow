# Changelog

All notable changes to StreamSnow are recorded here. This project follows
[semantic versioning](https://semver.org/) once it reaches its first release.

## [0.2.0] - 2026-06-30

### Fixed
- The scaffolded `branded_metric` now HTML-escapes its label/value/delta before
  rendering with `unsafe_allow_html=True`, so a database-derived value cannot
  inject markup into the viewer's page (hardening applied to the template and the
  example). Dependency-name matching is PEP 503-normalized, so a manifest that
  spells a package with underscores/dots (`snowflake_snowpark_python`) is no
  longer reported as missing.
- `validate-app` now validates the **contents** of the sibling dependency
  manifest, not just its presence: container apps must declare a
  `requires-python` that admits the container runtime's Python (PEP 440
  specifier semantics, so `>=3.10` is accepted and `<3.11` / `==3.10.*` are
  correctly rejected) plus `streamlit` + `snowflake-snowpark-python`; warehouse
  apps must declare those deps in `environment.yml` and must not pin `python`.
- `check caching` now flags two patterns it previously missed: a public loader
  that hands a **named query through a local variable**
  (`sql = load_sql("x"); conn.query(sql)`) and one that **delegates** a named
  query to a private fetch helper (including transitive helper chains). Only the
  SQL-bearing argument is inspected, so an unrelated string keyword (e.g.
  `query_tag="adhoc"`) no longer trips the generic-executor guard.

### Added
- Documentation guides: `docs/getting-started.md` (try the example with no
  Snowflake, then set up a governed repo), `docs/data-discovery.md` (find tables
  and wire governed queries), and `docs/deploying.md` (the end-to-end deploy
  story for both deploy sources). Linked from the README.
- Runnable example app at `examples/sample-dashboard/` — a StreamSnow-shaped
  Streamlit dashboard wired to deterministic sample data, so it renders with
  `streamlit run` and **no Snowflake connection**. Mirrors the `streamsnow init`
  structure (st.navigation entrypoint, branding, `@st.cache_data` loaders).
- `packaging` runtime dependency (PEP 440 version-specifier parsing in
  `validate-app`).

## [0.1.0] - 2026-06-27

Initial release.

### CLI (`streamsnow`)
- `configure` / `init` / `new` — set up the Snowflake environment and scaffold a
  governed monorepo + apps (container or warehouse runtime).
- `doctor` — machine + config prerequisite checks.
- `validate-app` — deterministic PASS/FAIL gate; `check schema-refs|security|caching|bind-predicates`.
- `preview` — run an app locally against live Snowflake.
- `deploy-setup` / `deploy-sql` / `stage-path` / `config-get` — deploy SQL + helpers
  for stage-copy and git-repository sources.
- `update` — re-render governance files from the current config (dry-run by default).

### Claude Code plugin
- 14 skills (onboard, refine-requirements, new-app, add-page, preview-app,
  validate-app, ship-app, start-app, review-app, deep-dive-data, apply-review,
  auto-review-app, sql-review, migrate-app) + 4 shared recipes.
- SessionStart hook, guarded to StreamSnow repos.

### Governance & safety
- Typed, validated `streamsnow.config.yaml` with an injection-safe rendering gate.
- Config-driven schema allow/deny, app-security, caching-TTL, and bind-predicate checks.
- Pre-publish privacy/export gate; generated repos ship pre-commit + CI guardrails.

### Packaging
- PyPI Trusted-Publishing release workflow; wheel-smoke + 3.11/3.12 CI matrix.
