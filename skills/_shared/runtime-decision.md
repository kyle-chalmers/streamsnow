# Choosing a runtime — container vs. warehouse

The one place this decision is explained. Skills link here instead of re-deriving it; if you are
reading this from a skill, take the answer and go back — don't restate this file to the user.

Both runtimes are fully supported ways to run a Streamlit app in Snowflake (container has been GA
since March 2026). Neither is "the right one" in general — the choice is a trade-off, and the
repo's configured default is the answer unless the app has a concrete reason to differ. Sources
for every fact below, with retrieved dates: `docs/snowflake-docs.md` in the StreamSnow repo.

## Where the choice lives

- **Repo default:** `runtime:` in `streamsnow.config.yaml` — set once by `streamsnow configure`.
- **Per-app choice:** the app's `snowflake.yml`. A **container** app declares a `runtime_name:` key
  (plus compute pool + external-access integration); a **warehouse** app declares none of those.
- **Recorded intent:** `REQUIREMENTS.md` §9, with a one-line justification when the app deviates
  from the repo default.

**Detecting the runtime:** match `runtime_name:` as an actual YAML key in `apps/<slug>/snowflake.yml`,
never a loose word-grep — apps often keep a "flip back to container" note in a comment, and a comment
match flips your whole understanding of the app.

## The trade-off

| | Container | Warehouse |
|---|---|---|
| Dependencies | PyPI, via `pyproject.toml` (PEP 440 pins, `pkg==1.2.3`) | Snowflake Anaconda channel, via `environment.yml` (conda pins, `pkg=1.2.3`); narrower and lags PyPI |
| Connection pattern | `st.connection("snowflake")` locally **and** deployed — never `get_active_session()`, which is warehouse-only and not thread-safe in the shared process | `get_active_session()` deployed; `st.connection` fallback for local runs |
| Local preview parity | High — same code path as deployed, so grant gaps surface locally | Lower — `get_active_session()` only exists inside Snowflake |
| Cold start | 1–3 min (image build/boot on a compute pool) | Effectively instant |
| Cost model | Compute pool; the server keeps running until 3 days pass with no viewer. `SYSTEM_COMPUTE_POOL_CPU` packs 3 apps per node, a custom pool runs 1 app per node | Warehouse credits per query; websocket sleeps after ~15 min idle by default |
| One-time Snowflake setup | Compute pool + PyPI access (an external-access integration today; Snowflake now prefers an artifact repository, and attaching both disables the EAI) must exist before first deploy | None beyond the warehouse itself |
| Shared state | One shared server process across viewers — module-level mutable state needs care; `st.cache_*` is shared across sessions | Isolated per-session execution; cache is per session |
| Platform limits | 200 MB message default (configurable); custom components v2 and static files supported | 32 MB message cap; components v2 and static files unsupported |

## How to choose

1. **Follow the repo default** (`runtime:` in config) unless the spec or the user gives a reason not to.
2. Reasons to pick **container**: the app needs a package that isn't on the Anaconda channel, or you
   want local preview to exercise the exact deployed code path.
3. Reasons to pick **warehouse**: cold-start latency matters (viewers open it rarely and briefly),
   compute-pool cost isn't justified, all deps are on the Anaconda channel, or the app carries
   cross-viewer module-level mutable state that is risky in a shared process.
4. Record any deviation from the default in §9 with the reason. Switching later is a re-deploy plus a
   manifest + connection-pattern rewrite — decide before scaffolding, not after.

For an already-deployed app, `snowflake.yml` only declares the *intended* runtime — the
authoritative answer is live: `SHOW STREAMLITS` / `DESC STREAMLIT <fqn>`. Verify the actual runtime
(and its Streamlit version) before diagnosing any feature-compatibility problem; container and
warehouse run very different Streamlit builds, and a diagnosis made against the wrong runtime
removes working features while fixing nothing.

## What follows from the choice (checklist)

- **Manifest dialect.** Container → `pyproject.toml` with PEP 440 pins. Warehouse → `environment.yml`
  with conda pins, and **never pin `python`** there — the warehouse supplies the interpreter, and a
  pinned one breaks the manifest. The validate gate checks the manifest matches the declared runtime.
- **Connection code.** Container → `conn = st.connection("snowflake")`, and pass `ttl=0` to
  `conn.query(...)` so the outer `@st.cache_data(ttl=...)` is the single source of truth — Streamlit
  issue #13644: `conn.query`'s internal cache ignores `params`, so two filters can share one stale
  result without `ttl=0`. Warehouse →
  `get_active_session()` when deployed, with the commented `st.connection` fallback for local runs —
  the fallback is a conscious local-dev toggle the developer owns; revert it before the PR.
- **Local preview.** Container apps run locally as-is. A warehouse app raises
  `get_active_session` errors outside Snowflake — that's the runtime's signature, not a code bug;
  use the fallback swap or verify in Snowsight.
- **Don't mix patterns within one app.** Match whatever the app's existing pages already do.
- **Governance is runtime-independent.** The schema allowlist, security, caching, and bind-predicate
  checks apply identically to both runtimes.

## Deploy-failure signatures (post-merge)

- **Warehouse** apps fail fast and loud — almost always a missing grant (`Insufficient privileges` /
  `not authorized`).
- **Container** apps add image-build and cold-start failure modes — a missing/suspended compute pool,
  a missing external-access integration blocking PyPI, or a verify step that outran a 1–3 min cold
  start while the app actually deployed fine. Translate specific errors via
  [deploy-error-translator.md](deploy-error-translator.md).
