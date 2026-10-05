# Principles

Every change to StreamSnow is judged against the [mission and vision](../README.md#mission)
and these nine principles. When two of them pull in different directions, the
discussion happens in the issue or PR, in the open. The numbers are stable: issues,
reviews and [docs/versioning.md](versioning.md) cite them by number.

1. **One implementation, many consumers:** CLI, plugin, pre-commit, and CI call the same code.
2. **Detection is automated and total; destruction requires explicit committed consent.**
3. **The backstop asks; it never decides:** the gates are `validate-app` and CI, not the review nudge.
4. **Org knowledge lives in `streamsnow.config.yaml` and `.streamsnow/`** (overlays and the house design guide), never in skills.
5. **Every rule names the incident that created it and the mechanism that enforces it.**
6. **Degrade, don't die:** a missing enabler is named, not refused.
7. **Faithful to a real fleet:** a check that fails a well-run production app is a defect in the check until proven otherwise (`tests/fixtures/fleet/` is the regression net).
8. **Leaving should be cheap:** everything StreamSnow writes is a plain file the repo keeps ([Ownership and exit](distribution.md#ownership-and-exit)).
9. **Design is a default, not a gate:** StreamSnow ships opinionated defaults with their reasons, the org's house style overrides them, and only correctness and governance block a ship.

## Where you can see them at work

| Principle | Where it shows up |
|---|---|
| 1. One implementation | Every check is a `streamsnow` subcommand; skills, hooks, pre-commit and CI call it ([CLI reference](cli-reference.md)) |
| 2. Consent for destruction | Renamed or removed apps are only dropped after a `deploy/tombstones.yml` entry in the same PR ([Deploying](deploying.md#retiring-or-renaming-an-app)) |
| 3. The backstop asks | The review nudge is warn-only; `validate-app` and CI are the gates ([README hooks](../README.md#hooks-in-full)) |
| 4. Org knowledge in config | `streamsnow.config.yaml` and `.streamsnow/overlays/` ([Getting started](getting-started.md#the-config-file)) |
| 5. Every rule names its incident | [Production lessons](production-lessons.md) and each check's docstring |
| 6. Degrade, don't die | `streamsnow doctor` separates `required` from `optional` checks |
| 7. Faithful to a real fleet | `tests/fixtures/fleet/`, the anonymized apps every check must pass |
| 8. Leaving is cheap | [Ownership and exit](distribution.md#ownership-and-exit) |
| 9. Design is a default | The shared design guides in `skills/_shared/` and repo overlays |
