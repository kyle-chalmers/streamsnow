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

## How StreamSnow handles secrets

**The five GitHub secrets** the deploy workflow reads, set by
`streamsnow ci-key push` in this order:

| Secret | Holds |
|---|---|
| `SNOWFLAKE_USER` | the CI service user, for example `STREAMSNOW_DEPLOY_USER` |
| `SNOWFLAKE_PRIVATE_KEY_RAW` | that user's private key (unencrypted PKCS#8); the only sensitive one |
| `SNOWFLAKE_WAREHOUSE` | the warehouse CI deploys with |
| `SNOWFLAKE_ROLE` | the CI deploy role |
| `SNOWFLAKE_ACCOUNT` | the account locator; set last, because it switches the deploy job on |

`SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` is read only for an encrypted key, which
`ci-key create` does not make.

**Where the key lives.** `~/.streamsnow-ci/`: the private key
`streamsnow_ci_rsa_key.p8` (mode 600), the public key `.pub`, and `secrets/`
with one file per secret (mode 600; the private-key entry is a link to the
`.p8`). `ci-key create` makes the directory at mode 700 (it warns, but does not
change it, if an existing one is looser), refuses a directory inside a git
repository, never overwrites an existing key, and never prints a value. `ci-key push` reads each file and passes it to `gh secret set`
on stdin, and never prints a secret value (on a failure it prints gh's error
with the values redacted); if one fails it stops before `SNOWFLAKE_ACCOUNT`.

**The key guard** (`hooks/secret_guard.py`, Claude Code `PreToolUse`) denies
any Bash, PowerShell, Read, Grep, Glob, Edit, Write or NotebookEdit call that
names `.streamsnow-ci` or `streamsnow_ci_rsa_key`, in any case and with either
path separator. It allows only a plain `streamsnow ci-key ...` or
`streamsnow deploy-setup ...` command, and only when it contains none of `|`,
`;`, `&&`, `||`, `<`, a newline, `$(`, a backtick, `(` or `)`, or any `&` other
than one leading PowerShell call operator. A `>` redirect into the key
directory is also denied, and so is a malformed or wrongly typed tool call that
names the key directory.
Its gaps: it matches text, so a command built to hide the path (shell
variables, globs) can get past it; it runs only in Claude Code and only for the
tools in its matcher, so something outside it, such as an MCP filesystem server,
is not covered; and if the hook cannot start at all, the call goes through.
It backs up `ci-key push`, which
is the main protection.

**Never printed** by any skill: `snow connection list`, MCP configuration,
connection files, `profiles.yml`, and `SNOWFLAKE_*` environment values. Setup
reads named keys through a filter instead.

**What Claude never runs:** the admin SQL from `streamsnow deploy-setup
--admin` (you or your Snowflake admin runs it, in Snowsight or with
`snow sql -f`), and any sign-in that needs your password.

**Rotating the key.** Move `~/.streamsnow-ci/streamsnow_ci_rsa_key.p8` and
`.pub` aside, run `streamsnow ci-key create`, regenerate the admin file with
`streamsnow deploy-setup --admin --public-key-file ~/.streamsnow-ci/streamsnow_ci_rsa_key.pub`,
have the admin run its `ALTER USER` statement, then run `streamsnow ci-key push`.

## Everything else

Bugs, questions and feature requests go to
[GitHub issues](https://github.com/kyle-chalmers/streamsnow/issues).
