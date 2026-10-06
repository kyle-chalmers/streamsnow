"""`streamsnow ci-key push`: secrets go from file to gh's stdin, never to argv or output."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from streamsnow import ci_key

KEY = (
    "-----BEGIN "
    + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw\n-----END "
    + "PRIVATE KEY-----\n"
)
VALUES = {
    "SNOWFLAKE_USER": "STREAMSNOW_DEPLOY_USER",
    "SNOWFLAKE_WAREHOUSE": "STREAMSNOW_WH",
    "SNOWFLAKE_ROLE": "STREAMSNOW_DEPLOY_ROLE",
    "SNOWFLAKE_ACCOUNT": "ab12345.us-east-1",
}


def _secrets_dir(tmp_path: Path, **overrides: str) -> Path:
    d = tmp_path / "ci"
    (d / "secrets").mkdir(parents=True)
    # A regular file, not create()'s symlink: push must read either, and
    # symlinks need privileges on Windows CI. Bytes, so Windows text mode
    # doesn't turn the PEM's \n into \r\n before the byte-for-byte check.
    (d / "secrets" / ci_key.PRIVATE_KEY_SECRET).write_bytes(KEY.encode("utf-8"))
    for name, value in {**VALUES, **overrides}.items():
        (d / "secrets" / name).write_text(value, encoding="utf-8")
    return d


class FakeGh:
    """Records every gh call; fails the calls named in fail_on."""

    def __init__(
        self, *, fail_on: tuple[str, ...] = (), auth_ok: bool = True, repo_ok: bool = True
    ):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.fail_on, self.auth_ok, self.repo_ok = fail_on, auth_ok, repo_ok

    def __call__(self, cmd, input=None, capture_output=True, check=False):
        self.calls.append((list(cmd), input))
        rc, out, err = 0, b"", b""
        if cmd[1:3] == ["auth", "status"] and not self.auth_ok:
            rc, err = 1, b"You are not logged into any GitHub hosts."
        elif cmd[1:3] == ["repo", "view"]:
            rc, out = (0, b"acme/apps\n") if self.repo_ok else (1, b"")
            if not self.repo_ok:
                err = b"no git remotes found"
        elif cmd[1:3] == ["secret", "set"] and cmd[3] in self.fail_on:
            rc, err = 1, b"HTTP 403: Resource not accessible (value was " + (input or b"") + b")"
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def secret_calls(self):
        return [(c, i) for c, i in self.calls if c[1:3] == ["secret", "set"]]


def _which(name):
    return f"/usr/bin/{name}"


def test_sets_all_five_in_order_with_values_on_stdin_only(tmp_path):
    gh = FakeGh()
    result = ci_key.push(_secrets_dir(tmp_path), run=gh, which=_which)
    calls = gh.secret_calls()
    assert [c[3] for c, _ in calls] == list(ci_key.SECRET_NAMES)
    assert calls[-1][0][3] == "SNOWFLAKE_ACCOUNT"
    sent = {c[3]: i for c, i in calls}
    assert sent[ci_key.PRIVATE_KEY_SECRET] == KEY.encode("utf-8")  # byte for byte
    for name, value in VALUES.items():
        assert sent[name] == value.encode("utf-8")
    every_argv = " ".join(" ".join(c) for c, _ in gh.calls)
    for value in [*VALUES.values(), "MIIEvQIBADAN"]:
        assert value not in every_argv
    assert result.done == list(ci_key.SECRET_NAMES)
    assert result.failed is None and result.repo == "acme/apps"


def test_trailing_newline_is_stripped_from_plain_values_only(tmp_path):
    gh = FakeGh()
    d = _secrets_dir(tmp_path, SNOWFLAKE_ACCOUNT="ab12345.us-east-1\n")
    ci_key.push(d, run=gh, which=_which)
    sent = {c[3]: i for c, i in gh.secret_calls()}
    assert sent["SNOWFLAKE_ACCOUNT"] == b"ab12345.us-east-1"
    assert sent[ci_key.PRIVATE_KEY_SECRET].endswith(b"\n")  # the key is never altered


def test_failure_stops_before_account_and_redacts_the_message(tmp_path):
    gh = FakeGh(fail_on=("SNOWFLAKE_WAREHOUSE",))
    result = ci_key.push(_secrets_dir(tmp_path), run=gh, which=_which)
    names = [c[3] for c, _ in gh.secret_calls()]
    assert "SNOWFLAKE_ACCOUNT" not in names
    assert result.failed == "SNOWFLAKE_WAREHOUSE"
    assert "STREAMSNOW_WH" not in result.message and "<redacted>" in result.message
    assert result.not_attempted == ["SNOWFLAKE_ROLE", "SNOWFLAKE_ACCOUNT"]


def test_missing_gh_sets_nothing(tmp_path):
    gh = FakeGh()
    with pytest.raises(ci_key.CiKeyError, match="gh"):
        ci_key.push(_secrets_dir(tmp_path), run=gh, which=lambda name: None)
    assert gh.calls == []


def test_gh_not_signed_in_sets_nothing(tmp_path):
    gh = FakeGh(auth_ok=False)
    with pytest.raises(ci_key.CiKeyError, match="gh auth login"):
        ci_key.push(_secrets_dir(tmp_path), run=gh, which=_which)
    assert gh.secret_calls() == []


def test_no_github_repo_sets_nothing(tmp_path):
    gh = FakeGh(repo_ok=False)
    with pytest.raises(ci_key.CiKeyError, match="GitHub repository"):
        ci_key.push(_secrets_dir(tmp_path), run=gh, which=_which)
    assert gh.secret_calls() == []


def test_missing_secret_file_points_at_ci_key_create(tmp_path):
    d = _secrets_dir(tmp_path)
    (d / "secrets" / "SNOWFLAKE_ROLE").unlink()
    gh = FakeGh()
    with pytest.raises(ci_key.CiKeyError, match="ci-key create"):
        ci_key.push(d, run=gh, which=_which)
    assert gh.secret_calls() == []


def test_empty_secret_file_is_refused(tmp_path):
    gh = FakeGh()
    with pytest.raises(ci_key.CiKeyError, match="SNOWFLAKE_USER"):
        ci_key.push(_secrets_dir(tmp_path, SNOWFLAKE_USER=""), run=gh, which=_which)
    assert gh.secret_calls() == []


def test_repo_is_positional_for_view_and_a_flag_for_secret_set(tmp_path):
    # `gh repo view` has no --repo flag; passing one makes it exit 1.
    gh = FakeGh()
    result = ci_key.push(_secrets_dir(tmp_path), repo="acme/other", run=gh, which=_which)
    views = [cmd for cmd, _ in gh.calls if cmd[1:3] == ["repo", "view"]]
    assert views and all(cmd[3] == "acme/other" and "--repo" not in cmd for cmd in views)
    # secret set gets the name gh resolved (the fake answers acme/apps).
    for cmd, _ in gh.secret_calls():
        assert cmd[-2:] == ["--repo", result.repo]


def test_secret_set_targets_the_resolved_repo_without_a_flag(tmp_path):
    gh = FakeGh()
    result = ci_key.push(_secrets_dir(tmp_path), run=gh, which=_which)
    views = [cmd for cmd, _ in gh.calls if cmd[1:3] == ["repo", "view"]]
    assert views and "--repo" not in views[0]
    for cmd, _ in gh.secret_calls():
        assert cmd[-2:] == ["--repo", result.repo]


def test_trailing_spaces_are_stripped_from_plain_values(tmp_path):
    gh = FakeGh()
    ci_key.push(_secrets_dir(tmp_path, SNOWFLAKE_ROLE="DEPLOY_ROLE  \n"), run=gh, which=_which)
    sent = {c[3]: i for c, i in gh.secret_calls()}
    assert sent["SNOWFLAKE_ROLE"] == b"DEPLOY_ROLE"


def test_short_values_are_redacted_too():
    msg = ci_key._redact(
        "role DEV and account ab12345.us-east-1 rejected", ["DEV", "ab12345.us-east-1"]
    )
    assert "DEV" not in msg and "ab12345" not in msg


def test_a_one_letter_value_does_not_rewrite_earlier_markers():
    assert ci_key._redact("bad token SECRET", ["SECRET", "e"]) == "bad tok<redacted>n <redacted>"


def _cli(*args: str):
    from typer.testing import CliRunner

    from streamsnow.cli import app

    return CliRunner().invoke(app, list(args))


def test_cli_prints_names_only(tmp_path, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ci_key.subprocess, "run", gh)
    monkeypatch.setattr(ci_key.shutil, "which", _which)
    res = _cli("ci-key", "push", "--dir", str(_secrets_dir(tmp_path)))
    assert res.exit_code == 0, res.output
    for name in ci_key.SECRET_NAMES:
        assert f"{name}: set" in res.output
    for value in [*VALUES.values(), "MIIEvQIBADAN"]:
        assert value not in res.output
    assert "acme/apps" in res.output


def test_cli_partial_failure_exits_1_and_names_what_was_not_set(tmp_path, monkeypatch):
    gh = FakeGh(fail_on=("SNOWFLAKE_ROLE",))
    monkeypatch.setattr(ci_key.subprocess, "run", gh)
    monkeypatch.setattr(ci_key.shutil, "which", _which)
    res = _cli("ci-key", "push", "--dir", str(_secrets_dir(tmp_path)))
    assert res.exit_code == 1
    assert "SNOWFLAKE_ROLE: failed" in res.output
    assert "SNOWFLAKE_ACCOUNT: not set" in res.output
    assert "STREAMSNOW_DEPLOY_ROLE" not in res.output


def test_cli_precheck_failure_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_key.shutil, "which", lambda name: None)
    res = _cli("ci-key", "push", "--dir", str(_secrets_dir(tmp_path)))
    assert res.exit_code == 2


def test_create_next_block_points_at_push(tmp_path):
    import shutil as _shutil

    if _shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    example = Path(__file__).resolve().parent.parent / "streamsnow.config.example.yaml"
    res = _cli("ci-key", "create", "--config", str(example), "--dir", str(tmp_path / "ci"))
    assert res.exit_code == 0, res.output
    assert "streamsnow ci-key push" in res.output
    assert "for s in" not in res.output
    assert "password manager" in res.output


# `push --config`: the files must still match the config before anything is set.
CONFIG_VALUES = {
    "SNOWFLAKE_USER": "STREAMSNOW_DEPLOY_USER",
    "SNOWFLAKE_WAREHOUSE": "STREAMSNOW_WH",
    "SNOWFLAKE_ROLE": "STREAMSNOW_DEPLOY_ROLE",
    "SNOWFLAKE_ACCOUNT": "ab12345.us-east-1",
}
EXAMPLE = Path(__file__).resolve().parent.parent / "streamsnow.config.example.yaml"


def test_expected_values_that_match_push_normally(tmp_path):
    gh = FakeGh()
    result = ci_key.push(_secrets_dir(tmp_path), run=gh, which=_which, expected=dict(CONFIG_VALUES))
    assert result.done == list(ci_key.SECRET_NAMES)


def test_expected_value_mismatch_refuses_before_any_gh_call(tmp_path):
    gh = FakeGh()
    d = _secrets_dir(tmp_path, SNOWFLAKE_WAREHOUSE="OLD_WH", SNOWFLAKE_ROLE="OLD_ROLE")
    with pytest.raises(ci_key.CiKeyError) as err:
        ci_key.push(d, run=gh, which=_which, expected=dict(CONFIG_VALUES))
    assert gh.calls == []
    msg = str(err.value)
    assert "SNOWFLAKE_WAREHOUSE" in msg and "SNOWFLAKE_ROLE" in msg
    assert "SNOWFLAKE_USER" not in msg
    for value in ("OLD_WH", "OLD_ROLE", "STREAMSNOW_WH", "STREAMSNOW_DEPLOY_ROLE"):
        assert value not in msg


def test_expected_comparison_ignores_surrounding_whitespace(tmp_path):
    gh = FakeGh()
    d = _secrets_dir(tmp_path, SNOWFLAKE_ROLE="STREAMSNOW_DEPLOY_ROLE\n")
    result = ci_key.push(d, run=gh, which=_which, expected=dict(CONFIG_VALUES))
    assert result.failed is None


def test_push_without_expected_does_not_compare(tmp_path):
    gh = FakeGh()
    d = _secrets_dir(tmp_path, SNOWFLAKE_ROLE="ANYTHING")
    assert ci_key.push(d, run=gh, which=_which).failed is None


def test_cli_push_config_mismatch_exits_2_names_only(tmp_path, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ci_key.subprocess, "run", gh)
    monkeypatch.setattr(ci_key.shutil, "which", _which)
    d = _secrets_dir(tmp_path, SNOWFLAKE_ROLE="OLD_ROLE")
    res = _cli("ci-key", "push", "--config", str(EXAMPLE), "--dir", str(d))
    assert res.exit_code == 2, res.output
    assert "SNOWFLAKE_ROLE" in res.output
    assert "OLD_ROLE" not in res.output and "STREAMSNOW_DEPLOY_ROLE" not in res.output
    assert gh.calls == []


def test_cli_push_config_match_says_so_and_pushes(tmp_path, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ci_key.subprocess, "run", gh)
    monkeypatch.setattr(ci_key.shutil, "which", _which)
    res = _cli("ci-key", "push", "--config", str(EXAMPLE), "--dir", str(_secrets_dir(tmp_path)))
    assert res.exit_code == 0, res.output
    assert "match this config" in res.output
    assert len(gh.secret_calls()) == len(ci_key.SECRET_NAMES)
    for value in CONFIG_VALUES.values():
        assert value not in res.output


def test_cli_push_account_override_is_compared_not_the_configs(tmp_path, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ci_key.subprocess, "run", gh)
    monkeypatch.setattr(ci_key.shutil, "which", _which)
    d = _secrets_dir(tmp_path, SNOWFLAKE_ACCOUNT="other-acct")
    res = _cli(
        "ci-key", "push", "--config", str(EXAMPLE), "--account", "other-acct", "--dir", str(d)
    )
    assert res.exit_code == 0, res.output


def test_create_next_step_separates_mkdir_from_the_redirect(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("needs openssl")
    res = _cli("ci-key", "create", "--config", str(EXAMPLE), "--dir", str(tmp_path / "ci"))
    assert res.exit_code == 0, res.output
    lines = [ln.strip() for ln in res.output.splitlines()]
    assert "mkdir -p .internal" in lines
    deploy = [ln for ln in lines if ln.startswith("streamsnow deploy-setup --admin")]
    assert len(deploy) == 1 and deploy[0].endswith("> .internal/admin-setup.sql")
    # The CI key guard denies a command naming ~/.streamsnow-ci that is chained.
    assert "&&" not in deploy[0] and ";" not in deploy[0] and "|" not in deploy[0]
