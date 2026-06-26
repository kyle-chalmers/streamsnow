---
name: validate-app
description: Deterministic PASS/FAIL ship gate for a StreamSnow app — runs `streamsnow validate-app <slug>` (files, schema-refs, app-security, bind-predicates, caching) and explains how to fix any failing check. Use when the user says "validate", "is this ready", "check my app", "validate <slug>", or before /ship-app.
---

# validate-app

Run the deterministic PASS/FAIL gate on an app and report exactly what fails and how to fix it.

## Steps

1. Resolve the slug. If omitted, list `apps/*/` and ask which one; confirm `apps/<slug>/` exists.
2. Run `streamsnow validate-app <slug>`. This is the single source of truth — do not re-derive its checks by hand.
3. If it exits PASS, report PASS and stop.
4. On FAIL, read the reported failing checks and, for each, re-run the matching focused gate to surface the exact offending lines:
   - schema-refs → `streamsnow check schema-refs apps/<slug>`
   - security → `streamsnow check security apps/<slug>`
   - caching → `streamsnow check caching apps/<slug>`
   - bind-predicates → `streamsnow check bind-predicates apps/<slug>`
   - file/layout failures → no sub-gate; cite the path the validator named.
5. For each failure, give a one-line fix tied to the cited file and line. Apply only mechanical, unambiguous fixes; surface anything judgment-bound for the user to decide.
6. After applying fixes, re-run `streamsnow validate-app <slug>` to confirm it flips to PASS. Repeat until clean or a finding needs a human call.
7. Report a terse summary: each check as PASS/FAIL, fixes applied, anything left for the user.

## Hand-offs

- Deeper qualitative concerns (SQL efficiency, UI patterns, spec drift) → run /review-app — this gate does not judge quality.
- Once PASS → run /ship-app to open the PR. This skill is the inline safety gate /ship-app runs before pushing.

## Done when

`streamsnow validate-app <slug>` exits PASS, and any FAIL has either been fixed-and-reverified or handed back to the user with a specific reason.
