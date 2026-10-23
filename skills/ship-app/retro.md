# System-evolution retro (always, even on a clean ship)

One question before closing: did anything go wrong or get re-done this ship? If so, **which
layer was insufficient** — the config (`streamsnow.config.yaml` / governance rules), a skill,
a check (`streamsnow validate-app` / CI), or the deploy path? Propose the concrete fix to
*that* artifact. If the gap is in StreamSnow itself, draft an issue for the plugin repo rather
than patching around it locally, and show the user the draft. A public issue can carry details
of their apps and data, so post it (`gh issue create`) only after they say yes to that draft,
and leave out table names, values and anything else they ask you to; otherwise hand them the
draft to file themselves. Fixing the layer, not the instance, is what compounds. (Ported from
ticketwright's /ship Phase C.)
