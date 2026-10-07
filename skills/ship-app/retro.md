# System-evolution retro (always, even on a clean ship)

One question before closing: did anything go wrong or get re-done this ship? If so, **which
layer was insufficient** — the config (`streamsnow.config.yaml` / governance rules), a skill,
a check (`streamsnow validate-app` / CI), or the deploy path? Propose the concrete fix to
*that* artifact. If the gap is in StreamSnow itself, file it against the plugin repo (issue or
a note the user can act on) rather than patching around it locally. Fixing the layer, not the
instance, is what compounds. (Ported from ticketwright's /ship Phase C.)
