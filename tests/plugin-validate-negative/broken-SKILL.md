---
description: Intentionally malformed frontmatter. Not a real skill.
name [THIS LINE HAS NO COLON so the YAML parser must reject it
---

# Negative-control fixture

This file exists to prove the plugin-validate CI job actually reads skill content.

It is copied over a real SKILL.md in a throwaway tree, and validation is then asserted to
FAIL. If validation passes with this file in place, the gate is not inspecting skills and
the job must go red.

Context: a root-directory target resolves to marketplace.json in preference to plugin.json,
so "claude plugin validate . --strict" silently validated nothing. See the plugin-validate
job in .github/workflows/ci.yml.
