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
import shutil
import subprocess
from pathlib import Path

KEY_BASENAME = "streamsnow_ci_rsa_key"
PRIVATE_KEY_SECRET = "SNOWFLAKE_PRIVATE_KEY_RAW"
SECRET_NAMES = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    PRIVATE_KEY_SECRET,
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_ROLE",
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
    elif path.stat().st_mode & 0o077:
        warnings.append(f"{path} is readable by other users; consider `chmod 700` on it.")


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

    values = {
        "SNOWFLAKE_ACCOUNT": account,
        "SNOWFLAKE_USER": user,
        "SNOWFLAKE_WAREHOUSE": warehouse,
        "SNOWFLAKE_ROLE": role,
    }
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
            if path.read_text().strip() != values[name]:
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
