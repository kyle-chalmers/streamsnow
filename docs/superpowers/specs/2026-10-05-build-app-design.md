# /build-app: design spec

**Status:** design agreed with Kyle on 2026-10-05. Implementation follows once the two in-flight
workstreams below have landed.
**Scope of this document:** what `/build-app` is for, how it is structured, and how it stays
aligned with the mission, vision and principles in the [README](../../../README.md). It changes no
code. The README edits it calls for ship in the same PR.

## 1. Why

`/start-app` takes an app from idea to an opened PR. It is good at getting an app to ship safely:
governed schemas, cached loaders, validate-clean, reviewed. But it says little about whether the
app is any good for the people who open it. Its only Streamlit and visualization guidance is
[page-conventions.md](../../../skills/_shared/page-conventions.md) (the four-block page contract,
the glossary module and data-anchored default dates) and the "UI / Streamlit patterns" reviewer
in [dimensions.md](../../../skills/review-app/dimensions.md). It is also a single-agent wizard:
its allowed-tools list has no subagent tool, so every page is built one after another in one
context.

This redesign does three things:

1. **Renames `/start-app` to `/build-app`.** "Build" says what the skill does. Once
   `/audit-lineage` is retired (see §8), it is also first in the skill list.
2. **Makes the build deterministic and subagent-driven.** Fixed phases, fixed agent briefs, files
   owned by exactly one agent, and pass/fail decided only by the `streamsnow` CLI and the existing
   gates.
3. **Grounds the build in Streamlit and visualization practice** through defaults an adopter can
   override, so apps come out fast, trustworthy, robust, decision-focused, self-explanatory and
   beautiful.

## 2. The six traits

These are what a reviewer, human or agent, looks for. They are review heuristics, not gates
(§3). Nothing on this list blocks a ship.

| Trait | Means | Evidence a reviewer looks for |
|---|---|---|
| **Fast** | The default view renders quickly and costs little warehouse time. | <ul><li>Every loader cached and keyed on its filter arguments.</li><li>Aggregation done in SQL, not pandas, with row caps on detail tables.</li><li>Multi-widget filters batched in `st.form`, independent widgets in `@st.fragment`.</li><li>Query count per page.</li><li>Timings from the planned `sql-review bench` command (§8) and a time-to-render reading under default filters during preview.</li></ul> |
| **Trustworthy** | Every number is correct and traceable. | <ul><li>The page's `sql_review` audit trail.</li><li>Glossary formulas: every ratio is `SUM(n) ÷ SUM(d)`.</li><li>A `Sources:` and `Data as of:` footer on every page.</li><li>Each KPI cross-checked once against a probe query.</li></ul> |
| **Robust** | Behaves the same deployed as locally, and fails visibly. | <ul><li>Existing checks clean: `page-imports`, `bind-predicates`, `caching`.</li><li>No `None` bound into SQL params.</li><li>Designed empty, stale-data and error states.</li><li>A walkthrough under default filters and under edge filters that return nothing.</li></ul> |
| **Decision-focused** | Each page answers a named business question. | <ul><li>REQUIREMENTS.md §4 names the decision each page supports.</li><li>KPIs are ordered by that question.</li><li>Layout runs KPIs → trend → breakdown → detail unless the house style says otherwise.</li></ul> |
| **Self-explanatory** | Someone can open it cold and use it without a demo. | <ul><li>The page caption states the question the page answers.</li><li>Every metric has `help=` from the glossary.</li><li>A "How to read this page" expander.</li><li>The cold-reader test (§4, phase 7).</li></ul> |
| **Beautiful** | The chart form fits the data, and the page reads as one designed system. | <ul><li>Chart type chosen for the question, not the default.</li><li>Clear visual hierarchy.</li><li>Consistent color per entity across pages.</li><li>Accessible contrast and a colorblind-safe palette.</li><li>Numbers formatted for reading (`1.2M`, `34.5%`, no `23.0` counts).</li></ul> |

"Functional" from the original brief is split into decision-focused and self-explanatory,
because they fail differently: a page can answer the right question and still need a demo, or be
perfectly clear about the wrong question. "Efficient" is part of fast.

## 3. Design is configurable: defaults, not rules

Whether a chart is right depends on the data, the audience and the house style, so design is
never a gate. Three layers apply; when they conflict, the more specific one wins:

1. **StreamSnow defaults.** Shipped in the plugin as `_shared/` guides (§5). Each default reads as
   "prefer X because Y", with Y being a fleet lesson where one exists, or else published practice.
   An adopter overriding a default can see what they are trading away.
2. **Repo house style.**
   - Structured values stay where they are today: the `brand:` block in `streamsnow.config.yaml`
     (font, `chart_sequence`, theme), validated by `_validate_brand` in `streamsnow/scaffolder.py`
     and rendered into each app's `branding.py`.
   - Everything that can't be a config key goes in an optional, committed
     **`.streamsnow/design.md`**: "we never use pie charts", "KPI cards compare year over year,
     not month over month", "our analysts want dense pages".
   - Every app-touching skill reads it before designing or reviewing a page. That covers
     `/build-app`, `/review-app`, `/feedback-app` and `/migrate-app`.
   - It follows the same precedence as overlays ([overlays.md](../../../skills/_shared/overlays.md)):
     it overrides the defaults but cannot weaken a gate.
3. **Per app.** REQUIREMENTS.md §2 (Audience & Use) records who reads the app and any style
   choices specific to it ("executive summary, mobile-first").

**What never moves:** correctness and governance. Allowed schemas, read-only SQL, caching,
import safety and the placeholder checks stay enforced by `streamsnow validate-app` and CI,
exactly as today.

**Why not warnings.** `validate-app` could print WARN lines for design departures, configured the
way `sql_review: {coverage: warn | fail}` is. That was considered and rejected. Design judgments
encoded as regex/AST checks would flag well-run fleet apps (against principle 7) and invite people
to satisfy the check instead of the reader.

## 4. Architecture: a deterministic orchestrator with bounded subagents

**Principle: the CLI and the existing gates decide pass/fail; agents produce artifacts against
fixed contracts.**

- **The orchestrator.** `skills/build-app/SKILL.md` stays within the 80-line front-page cap that
  `tests/test_plugin_surface.py` enforces. It only:
  - reads §11 state
  - dispatches agents
  - runs the gate commands
  - stops at checkpoints
- **Agent briefs.** Each agent has a fixed brief in the plugin's `agents/` directory, using the
  same format `/sql-review` introduces (§8). A brief names:
  - its inputs
  - the files it owns (no two agents write the same file)
  - the result it returns, with a fixed shape
  - the command the orchestrator runs to verify that result
- **Fix loops are bounded.** After a fixed number of rounds, an unresolved finding goes to the
  user at the next checkpoint instead of looping.
- **Without subagents** (another agent host, or none), each brief is run one after another by the
  host itself, per [other-agents.md](../../../skills/_shared/other-agents.md). A human can follow
  the same phases by hand (§7, vision).

### Phases

State stays in REQUIREMENTS.md §11, so `streamsnow check requirements` keeps its contract. New
phase names are added to the lifecycle values it recognizes.

1. **Preflight.** Run `streamsnow doctor`. If the CLI or the repo config is missing, hand off to
   `/onboard` (from the onboarding refactor, §8).
2. **Spec.** Write REQUIREMENTS.md as today, plus:
   - a *decision question* for each §4 page
   - an audience and style line in §2

   **Checkpoint 1:** the user confirms the one-screen summary.
3. **Discover.** The `data-scout` agent (read-only):
   - profiles every §3 object: grain, date range, row counts, sample values for filter tokens
   - fills in §3
   - seeds the app's `sql_review/index.yaml`
4. **Design.** The `app-designer` agent reads the three design layers (§3) and writes a per-page
   layout plan into §4 to §7:
   - each chart type, with the reason for it
   - the KPI hierarchy and comparisons
   - the copy for captions, `help=` text and the "How to read this page" expander

   **Checkpoint 1b:** a one-screen text wireframe per page. The user adjusts it before any code is
   written.
5. **Foundation.** The orchestrator alone, in order:
   1. `streamsnow new <domain> <function>`.
   2. The shared modules every page imports: `branding.py`, `pages/_glossary.py`,
      `pages/_time_controls.py` and `pages/_layout.py`. `_layout.py` holds the KPI row, chart
      card, empty state and "How to read this page" expander.

   Doing this once, before any page, is what lets pages be built in parallel without diverging.
6. **Build pages.** One `page-builder` agent per §4 page, run in parallel.
   - Each owns three things: `pages/<page>.py`, that page's `queries/*.sql`, and the page's
     `sql_review` file.
   - Each runs `streamsnow check schema-refs`, `caching`, `bind-predicates` and `page-imports` on
     its own files before returning.
   - Two pages that need the same query: the orchestrator assigns it to one of them in the design
     plan.
7. **Verify.** Three advisory agents run in parallel against a running preview. Their findings go
   to the page-builder that owns the file, for a bounded number of rounds:
   - **`perf-reviewer`:** reads the code and the timings from `sql-review bench` and the
     preview's time-to-render reading.
   - **`viz-critic`:** takes Playwright screenshots of each page and reviews them against the
     three design layers. It cites the default or house-style line behind each finding.
   - **`cold-reader`:** sees only the screenshots and the page text, never the spec or the code.
     For each page it must state what decision the page supports and what each KPI means. A wrong
     or missing answer is a finding against the page's copy, not against the reader.

   **Checkpoint 2:** the user clicks through every page, as today.
8. **Review and ship.**
   1. `streamsnow validate-app <slug>`.
   2. `/review-app` (the five existing reviewers).
   3. **Checkpoint 3.**
   4. `/ship-app`. Admin DDL from `deploy-setup --admin` is still shown to the user, never run.

## 5. Knowledge files (StreamSnow defaults)

These go in `skills/_shared/`, each default with its reason, and are linked from
`page-conventions.md` (whose four-block contract stays as is):

- **`streamlit-performance.md`**
  - `st.cache_data` keyed on filter arguments, and `st.cache_resource` for clients.
  - Aggregate in SQL, cap detail rows, and load only what's on screen.
  - `st.form` for filter batches, `@st.fragment` for widgets that shouldn't rerun the page, lazy
    tabs.
  - Container-runtime rules: one process is shared across viewers, so there are no import-time
    side effects.
- **`visualization-guide.md`**
  - Which chart answers which question:
    - trend → line
    - comparison → sorted bar
    - part of a whole → stacked or 100% bar (pie only for two or three parts)
    - distribution → histogram or box
    - relationship → scatter
  - KPI card anatomy: value, delta against a *named* comparison, optional sparkline.
  - Four or five KPIs per row at most.
  - Direct labels over legends.
  - Zero baselines for bars.
  - One color per entity across every page.
  - Colorblind-safe palettes.
  - Formatting helpers.
  - Layout hierarchy.
- **`explainability.md`**
  - The page caption is the decision question.
  - Metric help comes from the glossary.
  - The "How to read this page" expander.
  - Empty, stale and error states that say what happened and what to do.
  - Annotating notable points.
  - How the cold-reader test works.

Defaults are derived from the conventions in `tests/fixtures/fleet/` wherever the fleet has
settled a question, and the guides say which ones those are.

## 6. Template changes (`streamsnow/_templates/app/`)

- **`branding.py.j2`**
  - `fmt_number`, `fmt_currency` and `fmt_pct` helpers.
  - A KPI card with a colored delta and an optional sparkline.
  - Bump `_BRANDING_VERSION` so `check branding-parity` lists the apps left on the old copy.
  - The container runtime pins Streamlit 1.59.2 and the warehouse runtime 1.52.2
    (`WAREHOUSE_STREAMLIT_PIN`). `@st.fragment` exists on both. Before the sparkline is used,
    confirm the warehouse pin supports it, or degrade without it.
- **New stubs:** `pages/_layout.py`, `pages/_glossary.py` and `pages/_time_controls.py`, imported
  package-qualified (`from pages._layout import ...`) per the page-imports gotcha.
- **`overview.py.j2`:** demonstrates the conventions on its sample block. It is still replaced by
  the first real page.

Every generated file stays a plain file the repo owns, and no app imports anything from the
plugin at runtime (principle 8).

## 7. Alignment with the mission, vision and principles

Checked against the [README](../../../README.md#mission). The edits listed at the end of this
section ship in this PR.

**Mission.**
- The redesign fits "turning production lessons into scaffolding and checks".
- It goes beyond the mission as written in two ways:
  - It aims at apps that are fast, trustworthy and useful *to the people who read them*, which
    the mission never said.
  - It ships *guidance* as well as checks.
- The mission is updated to say both.

**Vision.**
- "A reviewer with Snowsight can re-run the SQL behind the numbers" is the trustworthy trait.
- The other five traits are about the app's readers, whom the vision didn't mention. It gains an
  end-user clause.
- "With or without an AI assistant" constrains this design: subagents are an accelerator, not the
  only path. Every phase stays followable one step at a time by a person or another agent, and
  the knowledge files and templates are useful to a human building a page by hand.

**Principles.**

1. **One implementation, many consumers.** Anything deterministic is a `streamsnow` command, not
   agent prose. Timing reuses `sql-review bench` and the preview capture from the SQL review
   redesign rather than re-implementing them.
2. **Detection is automated and total; destruction requires consent.** Unchanged. No new agent
   runs DDL or deploys.
3. **The backstop asks; it never decides.** Consistent: design findings are advisory, and
   `validate-app` and CI remain the only gates.
4. **Org knowledge lives in config and `.streamsnow/`.** `.streamsnow/design.md` is a new kind of
   org knowledge outside `.streamsnow/overlays/`, because it is shared across skills rather than
   tied to one. The principle is widened to name it.
5. **Every rule names its incident and mechanism.** Unchanged for rules. Design guidance is
   explicitly *defaults*, each with its reason and its mechanism (advisory review). The new
   principle 9 says so.
6. **Degrade, don't die.**
   - No subagents → run phases one after another.
   - No Playwright → skip `viz-critic` and `cold-reader`, and say so in the report.
   - No live connection → the static reviewers still run.
7. **Faithful to a real fleet.** Defaults come from fleet conventions where they exist. Advisory
   reviewers must not describe a well-run fleet app as broken.
8. **Leaving should be cheap.** `design.md`, `_layout.py` and the rest are plain files; apps never
   depend on the plugin at runtime.

**Where it fits next to a BI tool.** The README says StreamSnow can replace a BI tool for
internal analytics. Apps that read well without a demo are what make that claim credible, so
this redesign supports it.

**Edits in this PR:**
- README mission: add "apps that load fast, can be trusted, and help people make decisions" and
  "scaffolding, checks, and guidance".
- README vision: add "the people it's built for can open it, understand it, and act on it without
  a walkthrough".
- Principle 4: names `.streamsnow/` (overlays and the house design guide).
- New principle 9: "Design is a default, not a gate."
- CONTRIBUTING: "eight principles" becomes "nine principles".

## 8. Coordination with in-flight work

Two other workstreams touch the same files. This design depends on both and must land after them.

**Start-app skill refactor** (onboarding):
- **Part A** adds `ci-key push` and the key guard, and edits `skills/start-app/setup.md`.
- **Part B** (planned for 0.8.0) moves setup into a new `/onboard` skill.
- **Ask:** carry the `start-app` → `build-app` rename in Part B, so 0.8.0 is one breaking release
  with one tombstone round, and `--setup` leaves the skill at the same moment.

**SQL review redesign**
([kyle-chalmers/streamsnow#41](https://github.com/kyle-chalmers/streamsnow/pull/41),
`docs/superpowers/specs/2026-10-04-sql-review-redesign.md`):
- One `sql_review/NN_page_name.sql` per page plus `index.yaml`, a `/sql-review` skill that retires
  `/audit-lineage`, live `probe` / `run` / `bench` commands, plugin `agents/`, and preview capture
  with a Playwright walk.
- **Asks:**
  - Keep `bench` and the preview capture callable from `/build-app`'s verify phase.
  - Share one brief format for `agents/*.md`.
- Page-builders target the new per-page `sql_review` layout, so this design's implementation
  follows that redesign's phase 1.

**Shared edits.**
- Both workstreams bring back names listed in `RETIRED_NAMES` in `tests/test_plugin_surface.py`:
  `onboard` and `sql-review`.
- The rename adds `"start-app": "/build-app"` to it and changes `EXPECTED_SKILLS`.
- Whichever merges later rebases these edits and CHANGELOG `[Unreleased]`.

**Alphabetical order.** `audit-lineage` sorts before `build-app`, so `/build-app` is first in the
skill list once the SQL review redesign retires `/audit-lineage`.

### The rename (in Part B)

- `git mv skills/start-app skills/build-app`, and set `name: build-app` in the frontmatter.
- Update about 60 references:
  - `README.md`
  - `docs/*.md`, plus `docs/images/skills-flow.excalidraw`
  - the other skills and `skills/_shared/`
  - `hooks/session_start.sh` and `.github/workflows/ci.yml`
  - in `streamsnow/`: `agent_skills.py`, `cli.py`, and in `tools/`: `check_requirements.py`,
    `validate_app.py`, `doctor.py`
  - the repo templates
  - the tests: `test_plugin_surface.py`, `test_agent_skills.py`, `test_init.py`,
    `test_wizard_flags.py`, `test_doctor.py`, `test_session_start_hook.py`,
    `test_check_requirements.py` and `test_skill_cli_parity.py`
  - the fleet fixture's REQUIREMENTS.md
- Add a tombstone entry in `streamsnow/_templates/repo/tombstones.yml.j2`, so consumer repos with
  old `/start-app` copies are pointed at `/build-app`.
- **Done when** `grep -rn start-app` matches only CHANGELOG history, `RETIRED_NAMES` and the
  tombstone.

## 9. Implementation phases (after both workstreams land)

1. **Defaults and design layers.**
   - The three `_shared/` guides.
   - `.streamsnow/design.md` support in every app-touching skill.
   - The REQUIREMENTS.md §2 and §4 additions, plus the `check requirements` lifecycle values.
   - The template upgrades.
   - Tests:
     - the templates render and pass `validate-app` on a fresh scaffold
     - the fleet fixtures still validate clean
2. **Orchestration.**
   - The `agents/*.md` briefs: `data-scout`, `app-designer`, `page-builder`, `perf-reviewer`,
     `viz-critic`, `cold-reader`.
   - The phase rewrite of `SKILL.md` and its sub-files.
   - The verify loop.
   - `Agent` added to the skill's allowed-tools.
   - The sequential fallback documented in `other-agents.md`.
   - End-to-end trial: build an app on `examples/tpcds-demo` against Snowflake and compare it
     with the same app built by today's `/start-app`.

## 10. Open questions

- **Location and name of the house style guide.** `.streamsnow/design.md` is the proposal.
  `streamsnow init` could write a commented starter, or the file could stay opt-in with no
  starter.
- **Which skills read the design layers.** Recommended: `/feedback-app` and `/migrate-app` as
  well as `/build-app` and `/review-app`, so a live app's fixes and a ported app's conform step
  follow the same style.
- **Warehouse runtime support.** Whether its Streamlit pin (1.52.2) supports metric sparklines.
  The guides degrade if it doesn't.
- **Time-to-render reading.** Whether it is part of the SQL review redesign's preview capture or
  its own `streamsnow preview` verb. Under principle 1 it is a CLI command either way.
