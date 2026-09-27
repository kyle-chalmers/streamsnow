# Security policy

## Reporting a vulnerability

Report security problems privately, through GitHub's private vulnerability
reporting: open this repository's **Security** tab and choose **Report a
vulnerability**
([direct link](https://github.com/kyle-chalmers/streamsnow/security/advisories/new)).
Please do not open a public issue for a vulnerability.

A useful report names:

- the StreamSnow version (`streamsnow --version`) and how it is installed
  (PyPI package, Claude Code plugin, or both);
- the part affected: a CLI command, a governance check, a scaffold template,
  the generated CI or deploy workflows, or a plugin skill or hook;
- the steps that reproduce it and what an attacker gains.

Leave out credentials, Snowflake account identifiers and real data. A
fictional example is enough to show the problem.

The maintainer replies in the private advisory thread, and the fix ships in a
patch release recorded in the [CHANGELOG](CHANGELOG.md).

## Supported versions

Fixes land in the latest release. Upgrade with `uv tool upgrade streamsnow`,
then `streamsnow update` in each governed repo to re-render the governance
files.

## Scope

In scope: anything in this repository that runs on a user's machine, in CI, or
in Snowflake. Out of scope: a committer with write access deliberately
defeating a governance check. The checks catch accidents and drift, and
repository review remains the trust boundary for malicious commits (see the
read-only guard notes in `streamsnow/tools/sql_review.py`).

## Everything else

Bugs, questions and feature requests go to
[GitHub issues](https://github.com/kyle-chalmers/streamsnow/issues).
