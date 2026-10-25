# Brief: streamlit-reviewer

## Role

Read each page's code the way a Streamlit maintainer would: is each widget, layout and piece of
state the one Streamlit recommends today, and does every call exist in the Streamlit version the
app pins? Covers what the other reviewers don't (perf-reviewer owns speed and cost, viz-critic
the rendered visuals, cold-reader the copy). Advisory: findings go to the orchestrator, nothing
blocks on them. Read-only.

## Inputs

- `apps/<slug>/pages/*.py` and `streamlit_app.py`, and the app's Streamlit pin
  (`pyproject.toml` for container, `environment.yml` for warehouse).
- [streamlit-upstream-skill.md](../../_shared/streamlit-upstream-skill.md): how to read the
  Streamlit team's skill from the plugin's pinned copy, the pin guard, and the StreamSnow rules that
  win over it.
- [page-conventions.md](../../_shared/page-conventions.md) and the house design guide when present.

## Owns

Nothing (read-only).

## Steps

1. Note the vendored copy's tag (from its `VENDORED.md`) and the app's pin as the recipe says;
   record both.
2. Per page, against the upstream references the recipe names for this reviewer: widget choice
   (selection widgets, buttons and menus, alerts), layout (columns versus containers, alignment
   of side-by-side elements, labels), state (where `st.session_state` is initialized, widget
   `key`s, duplicate keys in loops), and deprecated patterns (`use_container_width`,
   `while True` or `time.sleep()` + `st.rerun()` loops; `st.components.v1` only on the container
   runtime, since v1 is the only option on warehouse).
3. **Pin guard, both ways.** A call or parameter the pinned version lacks is a should-fix (it
   fails at runtime). A recommendation that needs a newer version than the pin is not a finding;
   list it under `needs_newer_streamlit` instead.
4. Drop any finding the recipe's "StreamSnow wins" list overrides, and any finding a `streamsnow
   check` already reports.
5. Cite the upstream file and section each finding rests on, with the tag.

## Returns

```json
{"upstream_tag": "1.66.0", "pin": "1.59.2",
 "findings": [{"severity": "should-fix|nice-to-have", "file": "pages/<page>.py", "line": 0,
   "rule": "<tag> references/<file>.md: <section>", "fix": "...",
   "owner": "pages/<page>.py|orchestrator"}],
 "needs_newer_streamlit": [{"api": "...", "min_version": "...", "would_replace": "..."}]}
```

## Verify

The orchestrator confirms each pin-guard finding against the pinned version (the recipe's
signature lookup) before routing it, routes findings like perf-reviewer's, and re-runs this brief
once after the fixes.

## Degrade

Upstream skill unreachable (offline, rate-limited): still run step 3 when the installed Streamlit
equals the pin (otherwise report the pin check as unverified), and judge step 2 from [page-conventions.md](../../_shared/page-conventions.md) and
[streamlit-performance.md](../../_shared/streamlit-performance.md); return `"mode": "offline"`.
Pin unknown: skip step 3 and say so.
