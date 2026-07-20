# Choosing a runtime — container vs. warehouse

The one place this decision is explained. Skills link here instead of re-deriving it; if you are
reading this from a skill, take the answer and go back — don't restate this file to the user.

Both runtimes are fully supported ways to run a Streamlit app in Snowflake. Neither is "the right
one" in general — the choice is a trade-off, and the repo's configured default is the answer unless
the app has a concrete reason to differ.

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
| Connection pattern | `st.connection("snowflake")` locally **and** deployed | `get_active_session()` deployed; `st.connection` fallback for local runs |
| Local preview parity | High — same code path as deployed, so grant gaps surface locally | Lower — `get_active_session()` only exists inside Snowflake |
| Cold start | 1–3 min (image build/boot on a compute pool) | Effectively instant |
| Cost model | Compute pool (runs while the pool is up) | Warehouse credits per query |
| One-time Snowflake setup | Compute pool + external-access integration must exist before first deploy | None beyond the warehouse itself |
| Shared state | One shared server process across viewers — module-level mutable state needs care | Isolated per-session execution |

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
  `conn.query(...)` so the outer `@st.cache_data(ttl=...)` is the single source of truth (otherwise
  you get double caching and confusing staleness). Warehouse →
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
