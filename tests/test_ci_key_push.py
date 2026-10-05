"""`streamsnow ci-key push`: secrets go from file to gh's stdin, never to argv or output."""

from __future__ import annotations

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
    # symlinks need privileges on Windows CI.
    (d / "secrets" / ci_key.PRIVATE_KEY_SECRET).write_text(KEY, encoding="utf-8")
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


def test_repo_flag_is_passed_through(tmp_path):
    gh = FakeGh()
    ci_key.push(_secrets_dir(tmp_path), repo="acme/other", run=gh, which=_which)
    for cmd, _ in gh.calls:
        if cmd[1:3] in (["secret", "set"], ["repo", "view"]):
            assert cmd[-2:] == ["--repo", "acme/other"]
