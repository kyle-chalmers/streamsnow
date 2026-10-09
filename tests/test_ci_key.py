"""`streamsnow ci-key create`: key pair + CI secret files, never echoing a value."""

from __future__ import annotations

import base64
import hashlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from streamsnow import ci_key
from streamsnow.deploy import read_public_key

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"
ACCOUNT = "ab12345.us-east-1"  # the example config's placeholder locator


def _cli(*args: str):
    from typer.testing import CliRunner

    from streamsnow.cli import app

    return CliRunner().invoke(app, list(args))


def _create(tmp_path: Path, *extra: str):
    return _cli("ci-key", "create", "--config", str(EXAMPLE), "--dir", str(tmp_path / "ci"), *extra)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


#: Windows has no POSIX permission bits (st_mode reads 0o777/0o666 and chmod only
#: toggles read-only); the key directory is protected by the user profile's ACLs.
_POSIX = sys.platform != "win32"
posix_permissions = pytest.mark.skipif(not _POSIX, reason="POSIX permission bits")


def test_creates_the_layout_the_deploy_workflow_reads(tmp_path):
    res = _create(tmp_path)
    assert res.exit_code == 0, res.output
    d = tmp_path / "ci"
    p8, pub = d / "streamsnow_ci_rsa_key.p8", d / "streamsnow_ci_rsa_key.pub"
    if _POSIX:
        assert _mode(d) == 0o700 and _mode(d / "secrets") == 0o700
        assert _mode(p8) == 0o600
    assert "BEGIN PRIVATE KEY" in p8.read_text(
        encoding="utf-8"
    )  # unencrypted PKCS#8, as the workflow expects
    read_public_key(pub)  # a valid PEM public key for --public-key-file
    secrets = d / "secrets"
    assert sorted(p.name for p in secrets.iterdir()) == sorted(ci_key.SECRET_NAMES)
    link = secrets / "SNOWFLAKE_PRIVATE_KEY_RAW"
    assert link.is_symlink() and link.resolve() == p8.resolve()
    expected = {
        "SNOWFLAKE_ACCOUNT": ACCOUNT,
        "SNOWFLAKE_USER": "STREAMSNOW_DEPLOY_USER",
        "SNOWFLAKE_WAREHOUSE": "STREAMSNOW_WH",
        "SNOWFLAKE_ROLE": "STREAMSNOW_DEPLOY_ROLE",
    }
    for name, value in expected.items():
        assert (secrets / name).read_text(
            encoding="utf-8"
        ) == value  # no trailing newline for gh secret set
        assert not _POSIX or _mode(secrets / name) == 0o600


def test_output_never_contains_a_secret_value(tmp_path):
    res = _create(tmp_path)
    p8_body = (
        (tmp_path / "ci" / "streamsnow_ci_rsa_key.p8").read_text(encoding="utf-8").splitlines()[1]
    )
    assert p8_body not in res.output
    assert ACCOUNT not in res.output
    assert "PRIVATE KEY-----" not in res.output


def test_fingerprint_matches_snowflakes_rsa_public_key_fp(tmp_path):
    res = _create(tmp_path)
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(tmp_path / "ci" / "streamsnow_ci_rsa_key.pub"),
         "-outform", "DER"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    assert expected in res.output


def test_rerun_reuses_the_key_and_keeps_secret_files(tmp_path):
    first = _create(tmp_path)
    p8 = tmp_path / "ci" / "streamsnow_ci_rsa_key.p8"
    before = p8.read_bytes()
    (tmp_path / "ci" / "streamsnow_ci_rsa_key.pub").unlink()  # regenerated from the .p8
    second = _create(tmp_path, "--account", "other-acct")
    assert second.exit_code == 0, second.output
    assert p8.read_bytes() == before
    fp = [ln for ln in first.output.splitlines() if "fingerprint" in ln]
    assert fp and fp == [ln for ln in second.output.splitlines() if "fingerprint" in ln]
    assert "Reused key pair" in second.output
    # A differing existing secret is reported by name, never rewritten or echoed.
    assert (tmp_path / "ci" / "secrets" / "SNOWFLAKE_ACCOUNT").read_text(
        encoding="utf-8"
    ) == ACCOUNT
    assert "secrets/SNOWFLAKE_ACCOUNT differs" in second.output
    assert "other-acct" not in second.output


def test_mismatched_public_key_is_refused(tmp_path):
    _create(tmp_path / "a")
    _create(tmp_path / "b")
    shutil.copy(
        tmp_path / "b" / "ci" / "streamsnow_ci_rsa_key.pub",
        tmp_path / "a" / "ci" / "streamsnow_ci_rsa_key.pub",
    )
    res = _create(tmp_path / "a")
    assert res.exit_code == 2
    assert "does not match" in res.output


def test_missing_openssl_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_key.shutil, "which", lambda _name: None)
    res = _create(tmp_path)
    assert res.exit_code == 2
    assert "openssl is not on PATH" in res.output
    assert not (tmp_path / "ci").exists()


@posix_permissions
def test_existing_directory_is_never_repermissioned(tmp_path):
    d = tmp_path / "ci"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    res = _create(tmp_path)
    assert res.exit_code == 0, res.output
    assert _mode(d) == 0o755
    assert "readable by other users" in res.output
    assert _mode(d / "secrets") == 0o700  # created by the command, so locked down


def test_refuses_a_directory_inside_a_git_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    res = _create(tmp_path)
    assert res.exit_code == 2
    assert "inside a git repository" in res.output
    assert not (tmp_path / "ci").exists()


def test_refuses_a_symlinked_private_key(tmp_path):
    d = tmp_path / "ci"
    d.mkdir()
    elsewhere = tmp_path / "elsewhere.p8"
    (d / "streamsnow_ci_rsa_key.p8").symlink_to(elsewhere)  # dangling
    res = _create(tmp_path)
    assert res.exit_code == 2
    assert "symlink" in res.output
    assert not elsewhere.exists()


@posix_permissions
def test_kept_secret_files_are_tightened_to_600(tmp_path):
    _create(tmp_path)
    role = tmp_path / "ci" / "secrets" / "SNOWFLAKE_ROLE"
    role.chmod(0o644)
    assert _create(tmp_path).exit_code == 0
    assert _mode(role) == 0o600


def test_rerun_with_matching_files_says_all_match(tmp_path):
    _create(tmp_path)
    second = _create(tmp_path)
    assert second.exit_code == 0, second.output
    assert "Kept existing secrets/ (all match this config)" in second.output
    assert "differs" not in second.output


def test_rerun_flags_a_file_with_non_breaking_spaces(tmp_path):
    # push sends bytes.strip(), which keeps U+00A0, so create must not call it a match.
    _create(tmp_path)
    role = tmp_path / "ci" / "secrets" / "SNOWFLAKE_ROLE"
    role.write_text(" STREAMSNOW_DEPLOY_ROLE", encoding="utf-8")
    second = _create(tmp_path)
    assert "secrets/SNOWFLAKE_ROLE differs" in second.output
    assert "all match this config" not in second.output


def test_rerun_with_a_differing_file_does_not_claim_a_match(tmp_path):
    _create(tmp_path)
    second = _create(tmp_path, "--account", "other-acct")
    assert "all match this config" not in second.output
    assert "secrets/SNOWFLAKE_ACCOUNT differs" in second.output


def test_falls_back_to_a_private_copy_when_symlinks_are_refused(tmp_path, monkeypatch):
    """Native Windows without the symlink privilege raises on symlink_to (WinError 1314)."""

    def refuse(self, target, target_is_directory=False):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    res = _create(tmp_path)
    assert res.exit_code == 0, res.output
    d = tmp_path / "ci"
    p8 = d / "streamsnow_ci_rsa_key.p8"
    copy = d / "secrets" / "SNOWFLAKE_PRIVATE_KEY_RAW"
    assert copy.is_file() and not copy.is_symlink()
    assert copy.read_bytes() == p8.read_bytes()
    if _POSIX:
        assert _mode(copy) == 0o600
    assert "not a symlink" in res.output

    assert _create(tmp_path).exit_code == 0  # a re-run keeps the copy


def test_a_stale_private_key_copy_reads_as_mismatched(tmp_path, monkeypatch):
    def refuse(self, target, target_is_directory=False):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    assert _create(tmp_path).exit_code == 0
    d = tmp_path / "ci"
    res = ci_key.create(
        d,
        account=ACCOUNT,
        user="STREAMSNOW_DEPLOY_USER",
        warehouse="STREAMSNOW_WH",
        role="STREAMSNOW_CI_ROLE",
    )
    assert "SNOWFLAKE_PRIVATE_KEY_RAW" not in res.mismatched
    copy = d / "secrets" / "SNOWFLAKE_PRIVATE_KEY_RAW"
    copy.write_bytes(copy.read_bytes() + b"stale\n")
    res = ci_key.create(
        d,
        account=ACCOUNT,
        user="STREAMSNOW_DEPLOY_USER",
        warehouse="STREAMSNOW_WH",
        role="STREAMSNOW_CI_ROLE",
    )
    assert "SNOWFLAKE_PRIVATE_KEY_RAW" in res.mismatched


def test_private_key_copy_keeps_the_key_bytes_exactly(tmp_path, monkeypatch):
    """openssl on Windows can write CRLF PEM files; a text round trip would turn
    them into LF and every re-run would then report the copy as mismatched."""
    assert _create(tmp_path).exit_code == 0
    d = tmp_path / "ci"
    p8 = d / "streamsnow_ci_rsa_key.p8"
    p8.write_bytes(p8.read_bytes().replace(b"\n", b"\r\n"))
    (d / "secrets" / "SNOWFLAKE_PRIVATE_KEY_RAW").unlink()

    def refuse(self, target, target_is_directory=False):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    res = ci_key.create(
        d,
        account=ACCOUNT,
        user="STREAMSNOW_DEPLOY_USER",
        warehouse="STREAMSNOW_WH",
        role="STREAMSNOW_CI_ROLE",
    )
    assert (d / "secrets" / "SNOWFLAKE_PRIVATE_KEY_RAW").read_bytes() == p8.read_bytes()
    res = ci_key.create(
        d,
        account=ACCOUNT,
        user="STREAMSNOW_DEPLOY_USER",
        warehouse="STREAMSNOW_WH",
        role="STREAMSNOW_CI_ROLE",
    )
    assert "SNOWFLAKE_PRIVATE_KEY_RAW" not in res.mismatched
