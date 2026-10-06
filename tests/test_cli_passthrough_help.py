"""`streamsnow <group> <verb> --help` reaches the verb's own argparse parser.

Typer's eager ``--help`` on a passthrough command (one that forwards its raw
arguments to a standalone tool's argparse ``main``) swallowed ``--help`` before
the verb was seen, so ``streamsnow sql-review probe --help`` printed the group
docstring and none of the verb's flags. Passthrough commands set
``add_help_option=False`` so the verb's own help is what the user gets.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from streamsnow.cli import app

#: (command words, a flag only that verb's own parser defines)
VERB_HELP = [
    (["sql-review", "probe"], "--warehouse"),
    (["review-gate", "classify"], "--base-ref"),
    (["review-loop", "exit-condition"], "--max-iter"),
    (["migrate", "preflight"], "--target-slug"),
    (["preview", "start"], "--timeout"),
    (["agent-skills", "install"], "--agent"),
]


@pytest.mark.parametrize(("words", "flag"), VERB_HELP, ids=[" ".join(w) for w, _ in VERB_HELP])
def test_verb_help_shows_the_verbs_own_flags(words, flag):
    res = CliRunner().invoke(app, [*words, "--help"])
    assert res.exit_code == 0, res.output
    assert flag in res.output


@pytest.mark.parametrize(("words", "flag"), VERB_HELP, ids=[" ".join(w) for w, _ in VERB_HELP])
def test_short_help_flag_reaches_the_verb_too(words, flag):
    res = CliRunner().invoke(app, [*words, "-h"])
    assert res.exit_code == 0, res.output
    assert flag in res.output


@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_preview_shorthand_with_a_slug_shows_start_help(help_flag):
    # `preview <slug> --help` worked before the passthrough change: it is the
    # pre-0.6 shorthand for `preview start <slug> --help`.
    res = CliRunner().invoke(app, ["preview", "example-app", help_flag])
    assert res.exit_code == 0, res.output
    assert "--review-capture" in res.output and "--port" in res.output


@pytest.mark.parametrize("args", [["--help"], ["-h"], ["start", "--help"]])
def test_preview_help_without_a_slug_still_works(args):
    res = CliRunner().invoke(app, ["preview", *args])
    assert res.exit_code == 0, res.output
