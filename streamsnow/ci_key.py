"""Create the CI service user's key pair and the deploy workflow's secret files.

``streamsnow ci-key create`` writes, outside the repo (default ``~/.streamsnow-ci``)::

    streamsnow_ci_rsa_key.p8      unencrypted PKCS#8 private key (mode 600)
    streamsnow_ci_rsa_key.pub     its PEM public key, for deploy-setup --admin --public-key-file
    secrets/SNOWFLAKE_ACCOUNT     one file per repo secret, each holding exactly the value
    secrets/SNOWFLAKE_USER        (no trailing newline), so `gh secret set NAME < file`
    secrets/SNOWFLAKE_WAREHOUSE   stores it as-is
    secrets/SNOWFLAKE_ROLE
    secrets/SNOWFLAKE_PRIVATE_KEY_RAW -> ../streamsnow_ci_rsa_key.p8

The key is made with ``openssl`` (the tool Snowflake's key-pair guide uses), in
the same unencrypted PKCS#8 form, so no crypto dependency is added. An existing key is never overwritten,
and an existing secret file whose value differs from the config is left alone
and reported by name. Nothing here prints a secret value: only file names and
the public-key fingerprint, which is the same ``SHA256:`` value ``DESC USER``
shows as ``RSA_PUBLIC_KEY_FP``.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

KEY_BASENAME = "streamsnow_ci_rsa_key"
PRIVATE_KEY_SECRET = "SNOWFLAKE_PRIVATE_KEY_RAW"
# SNOWFLAKE_ACCOUNT last: it switches the deploy job on, so setting it before the
# others would make a merge in between fail at sign-in.
SECRET_NAMES = (
    "SNOWFLAKE_USER",
    PRIVATE_KEY_SECRET,
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_ROLE",
    "SNOWFLAKE_ACCOUNT",
)
DEFAULT_DIR = Path("~/.streamsnow-ci")


class CiKeyError(Exception):
    """User-facing failure; the message never contains key material."""


@dataclasses.dataclass
class CiKeyResult:
    directory: Path
    private_key: Path
    public_key: Path
    fingerprint: str
    key_created: bool
    written: list[str]
    kept: list[str]
    mismatched: list[str]
    warnings: list[str] = dataclasses.field(default_factory=list)


def _openssl(*args: str) -> bytes:
    """Run openssl with no secret on argv; return stdout (public data only)."""
    proc = subprocess.run(["openssl", *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise CiKeyError(f"`openssl {args[0]}` failed (exit {proc.returncode}).")
    return proc.stdout


def fingerprint(public_der: bytes) -> str:
    """Snowflake's ``RSA_PUBLIC_KEY_FP`` for a DER-encoded public key."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(public_der).digest()).decode()


def _write_private(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)


def _inside_git_worktree(path: Path) -> bool:
    return any((p / ".git").exists() for p in (path, *path.parents))


def _ensure_private_dir(path: Path, warnings: list[str]) -> None:
    """Create ``path`` at mode 700; an existing directory is never re-permissioned."""
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)  # mkdir's mode is filtered by the umask
    elif not path.is_dir():
        raise CiKeyError(f"{path} exists and is not a directory.")
    # POSIX only: Windows reports every directory as 0o777 and protects it with
    # ACLs instead, so this warning would fire on every run there.
    elif os.name == "posix" and path.stat().st_mode & 0o077:
        warnings.append(f"{path} is readable by other users; consider `chmod 700` on it.")


def config_values(*, account: str, user: str, warehouse: str, role: str) -> dict[str, str]:
    """The four plain-text secrets a config implies, keyed by secret name."""
    return {
        "SNOWFLAKE_ACCOUNT": account,
        "SNOWFLAKE_USER": user,
        "SNOWFLAKE_WAREHOUSE": warehouse,
        "SNOWFLAKE_ROLE": role,
    }


def create(
    directory: Path,
    *,
    account: str,
    user: str,
    warehouse: str,
    role: str,
) -> CiKeyResult:
    if shutil.which("openssl") is None:
        raise CiKeyError(
            "openssl is not on PATH. Install it (macOS and most Linux distributions ship it; "
            "on Windows use Git Bash's openssl), or follow Snowflake's key-pair guide by hand."
        )
    directory = Path(directory).expanduser().absolute()
    if _inside_git_worktree(directory):
        raise CiKeyError(
            f"{directory} is inside a git repository. Keep the private key OUTSIDE any repo "
            "(the default is ~/.streamsnow-ci)."
        )
    warnings: list[str] = []
    secrets = directory / "secrets"
    _ensure_private_dir(directory, warnings)
    _ensure_private_dir(secrets, warnings)

    p8 = directory / f"{KEY_BASENAME}.p8"
    pub = directory / f"{KEY_BASENAME}.pub"
    for path in (p8, pub):
        if path.is_symlink():
            raise CiKeyError(
                f"{path} is a symlink; refusing to write or trust a key through it. "
                "Replace it with the real file."
            )
    key_created = False
    if not p8.exists():
        old_umask = os.umask(0o077)
        try:
            _openssl(
                "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                "-out", str(p8),
            )  # fmt: skip
        finally:
            os.umask(old_umask)
        key_created = True
    p8.chmod(0o600)
    if not pub.exists():
        _openssl("pkey", "-in", str(p8), "-pubout", "-out", str(pub))

    # The fingerprint comes from the .pub that goes into the SQL, and the .pub
    # must belong to the .p8 CI signs with, or every deploy fails auth.
    pub_der = _openssl("pkey", "-pubin", "-in", str(pub), "-outform", "DER")
    if _openssl("pkey", "-in", str(p8), "-pubout", "-outform", "DER") != pub_der:
        raise CiKeyError(
            f"{pub.name} does not match {p8.name}. Move one of them aside and re-run to "
            "regenerate the public key from the private key."
        )

    values = config_values(account=account, user=user, warehouse=warehouse, role=role)
    written: list[str] = []
    kept: list[str] = []
    mismatched: list[str] = []
    for name in SECRET_NAMES:
        path = secrets / name
        if name == PRIVATE_KEY_SECRET:
            target = Path("..") / p8.name
            if path.is_symlink() or path.exists():
                kept.append(name)
                if path.resolve() != p8.resolve():
                    mismatched.append(name)
            else:
                path.symlink_to(target)
                written.append(name)
            continue
        if path.exists():
            kept.append(name)
            if path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077:
                path.chmod(0o600)  # a secret file never stays readable by others
            if path.read_text(encoding="utf-8").strip() != values[name]:
                mismatched.append(name)
        else:
            _write_private(path, values[name])
            written.append(name)

    return CiKeyResult(
        directory=directory,
        private_key=p8,
        public_key=pub,
        fingerprint=fingerprint(pub_der),
        key_created=key_created,
        written=written,
        kept=kept,
        mismatched=mismatched,
        warnings=warnings,
    )


@dataclasses.dataclass
class PushResult:
    repo: str
    done: list[str]
    failed: str | None = None
    message: str = ""
    not_attempted: list[str] = dataclasses.field(default_factory=list)


def _redact(text: str, values: list[str]) -> str:
    """Strip every line of every secret value out of ``text`` (gh's error output).

    Longest first, so a short value (a role such as ``DEV``) can't split a longer
    one before it is replaced. Short values are redacted too: an over-redacted
    error message is the safe failure.
    """
    lines = {ln.strip() for value in values for ln in value.splitlines()} - {""}
    # NUL-delimited placeholders first, so a short value can't rewrite an
    # earlier marker; gh's output never contains NUL.
    for i, line in enumerate(sorted(lines, key=len, reverse=True)):
        text = text.replace(line, f"\x00{i}\x00")
    return re.sub(r"\x00\d+\x00", "<redacted>", text)


def mismatched_secrets(directory: Path, expected: dict[str, str]) -> list[str]:
    """Names of existing secret files whose value differs from ``expected``.

    Compared stripped, the way ``create`` compares and ``push`` sends. A file
    that does not exist or cannot be read is skipped here: ``push`` reports it
    on its own. Only names are returned, never a value.
    """
    secrets = Path(directory).expanduser().absolute() / "secrets"
    out: list[str] = []
    for name in SECRET_NAMES:
        if name not in expected:
            continue
        path = secrets / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if text.strip() != expected[name]:
            out.append(name)
    return out


def push(
    directory: Path,
    *,
    repo: str | None = None,
    expected: dict[str, str] | None = None,
    run=None,
    which=None,
) -> PushResult:
    """Set the five deploy secrets on GitHub, each value going file -> gh's stdin.

    Checks everything before setting anything: gh present and signed in, the
    target repo resolvable, every secret file present and non-empty. Secrets go
    in SECRET_NAMES order, so SNOWFLAKE_ACCOUNT (which switches the deploy job
    on) is last, and a failure stops before it. No value is ever placed on argv
    or returned in a message.

    ``expected`` (the values a config implies, see ``config_values``) guards the
    case where ``create`` kept an old secret file: the files are compared by
    name before any ``gh`` call, and a difference refuses the push, so a stale
    warehouse or role never reaches GitHub and fails the first deploy.
    """
    if expected:
        stale = mismatched_secrets(directory, expected)
        if stale:
            raise CiKeyError(
                "These secret files differ from the config, so nothing was pushed: "
                f"{', '.join(stale)}. Delete the named files under secrets/ and re-run "
                "`streamsnow ci-key create`, or fix the config."
            )
    run = run or subprocess.run
    which = which or shutil.which
    if which("gh") is None:
        raise CiKeyError(
            "gh (the GitHub CLI) is not on PATH. Install it, then run `gh auth login`."
        )
    if run(["gh", "auth", "status"], capture_output=True, check=False).returncode != 0:
        raise CiKeyError(
            "gh is not signed in. Run `gh auth login`, then re-run `streamsnow ci-key push`."
        )
    # `gh repo view` takes the repository as a positional argument, not --repo.
    view = run(
        [
            "gh",
            "repo",
            "view",
            *([repo] if repo else []),
            "--json",
            "nameWithOwner",
            "-q",
            ".nameWithOwner",
        ],
        capture_output=True,
        check=False,
    )
    target = view.stdout.decode("utf-8", errors="replace").strip()
    if view.returncode != 0 or not target:
        raise CiKeyError(
            "No GitHub repository found for this directory. Add a GitHub remote, or pass "
            "--repo owner/name."
        )

    secrets = Path(directory).expanduser().absolute() / "secrets"
    payloads: dict[str, bytes] = {}
    for name in SECRET_NAMES:
        path = secrets / name
        if not path.exists():
            raise CiKeyError(f"{path} is missing. Run `streamsnow ci-key create` first.")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CiKeyError(f"{path} cannot be read ({exc.strerror}).") from None
        if name != PRIVATE_KEY_SECRET:
            # A hand-edited file may end in a newline or stray spaces; create()
            # compares these files stripped, so push sends them stripped too.
            data = data.strip()
        if not data.strip():
            raise CiKeyError(
                f"secrets/{name} is empty. Delete it and re-run `streamsnow ci-key create`."
            )
        payloads[name] = data
    plain = [v.decode("utf-8", errors="replace") for v in payloads.values()]

    done: list[str] = []
    for i, name in enumerate(SECRET_NAMES):
        proc = run(
            # Always the resolved repo, so the one printed is the one written.
            ["gh", "secret", "set", name, "--repo", target],
            input=payloads[name],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raw = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            return PushResult(
                repo=target,
                done=done,
                failed=name,
                message=_redact(raw, plain) or f"gh exited {proc.returncode}",
                not_attempted=list(SECRET_NAMES[i + 1 :]),
            )
        done.append(name)
    return PushResult(repo=target, done=done)
