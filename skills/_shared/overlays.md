# Repo overlays — project-level augmentation of the plugin's skills

Every skill's first move is the same: **if `.streamsnow/overlays/<skill-name>.md`
exists in the repo, read it before executing the skill's steps.** An overlay is
committed, repo-owned prose that extends or overrides the generic procedure for
THIS repo — org-specific knowledge stays in the org's repo, reviewed in the
org's PRs, and survives every plugin upgrade untouched. `overlays/all.md`, when
present, applies to every skill and is read first.

## What belongs in an overlay

The generic skill is the procedure; the overlay is the local knowledge the
procedure can't ship with:

- extra steps ("our onboarding also installs these MCP servers", "seed the
  browser profile through SSO before any walkthrough");
- local failure signatures ("a deploy error naming this compute pool means
  ask the platform team, not a retry");
- environment specifics ("preview must run under this role so local matches
  the deployed caller"), house conventions, escalation contacts;
- overrides, stated explicitly ("skip step N here because …" — say why, so a
  future reader can retire the override when the reason dies).

**What does not belong:** secrets or credentials (never), generic improvements
(upstream those to the plugin — everyone should get them), and duplicate copies
of what AGENTS.md already says (AGENTS.md is always in context; an overlay is
for skill-specific depth AGENTS.md shouldn't carry).

## Precedence and conflicts

Overlay instructions win over the generic skill text when they conflict — that
is their purpose — but they cannot disable safety behavior that lives in code:
hooks, `streamsnow validate-app`, the read-only guarantees of `sql-review`, and
CI gates are unaffected by overlay prose. An overlay that contradicts a safety
gate is a mistake to surface to the user, not to obey.

## Authoring

Create `.streamsnow/overlays/` (committed — the generated `.gitignore` excludes
only `.streamsnow/preview/`), one file per skill, named exactly after the skill
(`ship-app.md`, `review-app.md`, …). Keep each overlay in the skill's own shape:
"Before step N", "After step N", "Instead of step N", or a plain "Also know"
section. Short beats complete — the skill still runs without it.
