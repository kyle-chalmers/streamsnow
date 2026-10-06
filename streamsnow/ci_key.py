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

``streamsnow ci-key verify`` signs in with those files the way the deploy job
does and checks, read-only, what CI will see (see :func:`verify`).
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

from .config import Config, ConfigError, quote_sql_literal, validate_identifier
from .deploy import ci_user_name, expected_ci_grants
from .policy import SchemaPolicy

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


def _read_secret_payloads(directory: Path) -> dict[str, bytes]:
    """The five secret values, keyed by name, exactly as the deploy job receives them.

    Every file must exist and be non-empty. The plain-text values are stripped
    (a hand-edited file may end in a newline or stray spaces, and ``create``
    compares these files stripped); the private key is sent byte for byte.
    Errors name the file, never its contents.
    """
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
            data = data.strip()
        if not data.strip():
            raise CiKeyError(
                f"secrets/{name} is empty. Delete it and re-run `streamsnow ci-key create`."
            )
        try:
            # Every consumer needs text: GitHub stores secrets as UTF-8 and
            # verify passes them as environment variables.
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise CiKeyError(
                f"secrets/{name} is not UTF-8 text. Delete it and re-run "
                "`streamsnow ci-key create`."
            ) from None
        payloads[name] = data
    return payloads


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

    payloads = _read_secret_payloads(directory)
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


# --- ci-key verify ----------------------------------------------------------------

_SNOW_ARGS = (
    "sql",
    "--stdin",
    "--format",
    "json",
    "--enable-templating",
    "NONE",
    "--temporary-connection",
)
_SNOW_MISSING = (
    "the Snowflake CLI (`snow`) is not on PATH; install it with `uv tool install snowflake-cli`."
)
WITHHELD = "snow failed; its message named the account, user or key and was withheld"
_BOX_AND_SPACE_RE = re.compile(r"[\s│╭╮╰╯─]+")


@dataclasses.dataclass
class Probe:
    """One check, named by the object it checked. ``detail`` never holds a secret."""

    probe: str
    object: str
    status: str  # pass | fail | skipped
    detail: str = ""


@dataclasses.dataclass
class VerifyResult:
    probes: list[Probe]

    @property
    def ok(self) -> bool:
        return all(p.status != "fail" for p in self.probes)

    def to_json(self) -> dict:
        return {"ok": self.ok, "probes": [dataclasses.asdict(p) for p in self.probes]}


def ci_env(payloads: dict[str, bytes], environ=None) -> dict[str, str]:
    """The environment the deploy job signs in with, built on top of ``environ``.

    Every inherited ``SNOWFLAKE_*`` variable and ``PRIVATE_KEY_PASSPHRASE`` is
    dropped first: a developer's own connection settings (a password, a default
    connection, an SSO authenticator, another role) would otherwise leak into
    the sign-in or quietly replace the CI identity, and verify would prove the
    wrong thing. Then the five secrets go in, plus ``SNOWFLAKE_JWT``.
    """
    base = os.environ if environ is None else environ
    env = {
        k: v
        for k, v in base.items()
        if not k.upper().startswith("SNOWFLAKE_") and k.upper() != "PRIVATE_KEY_PASSPHRASE"
    }
    for name in SECRET_NAMES:
        env[name] = payloads[name].decode("utf-8")
    env["SNOWFLAKE_AUTHENTICATOR"] = "SNOWFLAKE_JWT"
    # Wide enough that snow's Rich error panel never wraps a value across lines,
    # where exact-text redaction could not find it. Sign-in ignores it.
    env["COLUMNS"] = "1000"
    return env


def _mask_variants(payloads: dict[str, bytes]) -> list[str]:
    """Every form of the key, account and user an error message might echo.

    ``_redact`` matches exact text, and Snowflake errors print the account in
    lower case inside a hostname, with ``_`` as ``-``, and sometimes as the
    locator without its region.
    """
    values: list[str] = []
    account = payloads["SNOWFLAKE_ACCOUNT"].decode("utf-8", errors="replace")
    for value in (
        payloads[PRIVATE_KEY_SECRET].decode("utf-8", errors="replace"),
        payloads["SNOWFLAKE_USER"].decode("utf-8", errors="replace"),
        account,
        account.split(".")[0],
    ):
        for form in (value, value.replace("_", "-")):
            values += [form, form.lower(), form.upper()]
    return values


def _squash(text: str) -> str:
    """Lower-cased, without whitespace or box-drawing, with ``-`` and ``_`` the same."""
    return _BOX_AND_SPACE_RE.sub("", text).lower().replace("-", "_")


def _safe_detail(detail: str, payloads: dict[str, bytes]) -> str:
    """``detail`` unchanged, or :data:`WITHHELD` when any secret survived redaction.

    The backstop for what exact-text redaction misses: a value wrapped across
    a panel's lines, re-cased, or spelled with ``-`` for ``_``. It fails closed,
    so a false match costs an error message and never leaks a value.
    """
    squashed = _squash(detail)
    needles: list[str] = []
    for name in (PRIVATE_KEY_SECRET, "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER"):
        text = payloads[name].decode("utf-8", errors="replace")
        needles += [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("-----")]
    account = payloads["SNOWFLAKE_ACCOUNT"].decode("utf-8", errors="replace")
    needles.append(account.split(".")[0])
    for needle in needles:
        squashed_needle = _squash(needle)
        if squashed_needle and squashed_needle in squashed:
            return WITHHELD
    return detail


def _select_probe_sql(cfg: Config, obj: str) -> str:
    """``SELECT * FROM <obj> LIMIT 0``, refused unless ``obj`` is in an allowed schema."""
    from .sf_exec import SnowError, guard  # noqa: PLC0415  (sf_exec imports the tools)

    gov = cfg.governance
    parts = obj.split(".")
    if len(parts) != 3:
        raise CiKeyError(f"--object {obj!r} must be DATABASE.SCHEMA.OBJECT.")
    try:
        for part in parts:
            validate_identifier(part, "--object")
    except ConfigError as exc:
        raise CiKeyError(str(exc)) from None
    db, schema, _ = parts
    policy = SchemaPolicy.from_governance(gov)
    if db.upper() != gov.database.upper() or not policy.is_allowed(schema):
        raise CiKeyError(
            f"--object {obj} is outside the governance allowlist ({gov.database}, schemas "
            f"{', '.join(gov.schema_allow)}); nothing was sent to Snowflake."
        )
    sql = f"SELECT * FROM {obj} LIMIT 0"
    try:
        guard(sql, policy)
    except SnowError as exc:
        raise CiKeyError(str(exc)) from None
    return sql


def verify(
    directory: Path,
    *,
    cfg: Config,
    obj: str | None = None,
    run=None,
    which=None,
    environ=None,
) -> VerifyResult:
    """Sign in as the CI service user with the CI key and check, read-only, what CI sees.

    Why this exists: without it, the first proof that the CI key, user, role,
    warehouse and grants line up is the first deploy after a merge, and a
    failure there is a red X on main with a CI log to dig through. This signs
    in exactly the way the deploy job does (``deploy.yml.j2``: the five secrets
    as ``SNOWFLAKE_*`` variables, ``SNOWFLAKE_AUTHENTICATOR=SNOWFLAKE_JWT``,
    ``snow ... --temporary-connection``) and runs only read-only probes, so
    onboarding can show what CI will see before any merge. The trade-off,
    accepted on purpose: this is the production CI credential used from a
    laptop, so the sign-in shows in the CI user's login history, and a network
    policy that only admits the CI runners refuses it.

    Everything that can be refused is refused before the first sign-in: a
    missing, empty or non-UTF-8 secret file, a user, warehouse or role file that no longer
    matches the config, an ``obj`` outside the governance allowlist, no
    ``snow`` on PATH. Then each probe is its own ``snow sql`` call, with the SQL
    on stdin and the secrets only in ``env=``:

    1. ``role``: ``CURRENT_ROLE()`` is the CI role;
    2. ``warehouse``: ``USE WAREHOUSE`` works;
    3. ``schema``: the app (and stage) schema is visible;
    4. ``grant``: each grant :func:`deploy.expected_ci_grants` reads out of the
       admin script is on ``SHOW GRANTS TO ROLE``;
    5. ``select``: a guarded ``LIMIT 0`` read of ``obj`` (skipped without one).

    A failed first probe means the sign-in itself failed, so the rest are
    skipped instead of signing in four more times. Error text goes through
    ``sf_exec._error_detail`` (frame stripped, quoted values masked) and
    ``_redact`` (key, account and user removed), then :func:`_safe_detail`
    withholds it whole if any of them is still recognizable. A ``snow`` call
    that cannot start, times out or prints unreadable output raises
    :class:`CiKeyError` (exit 2, no probe results), at any probe.
    """
    from .sf_exec import (  # noqa: PLC0415
        _LOGIN_ALLOWANCE_S,
        DEFAULT_TIMEOUT_S,
        SnowError,
        _error_detail,
        first_row,
        parse_output,
        upper_rows,
    )

    o = cfg.snowflake.objects
    ci = cfg.snowflake.roles.ci_role
    payloads = _read_secret_payloads(directory)
    stale = mismatched_secrets(
        directory,
        {
            "SNOWFLAKE_USER": ci_user_name(ci),
            "SNOWFLAKE_WAREHOUSE": o.default_warehouse,
            "SNOWFLAKE_ROLE": ci,
        },
    )
    if stale:
        raise CiKeyError(
            "These secret files differ from the config, so nothing was checked: "
            f"{', '.join(stale)}. Fix the config, or delete the named files under secrets/ "
            "and re-run `streamsnow ci-key create`."
        )
    select_sql = _select_probe_sql(cfg, obj) if obj else None
    grants = expected_ci_grants(cfg)
    if (which or shutil.which)("snow") is None:
        raise CiKeyError(_SNOW_MISSING)
    run = run or subprocess.run
    env = ci_env(payloads, environ)
    secret_text = _mask_variants(payloads)
    timeout = DEFAULT_TIMEOUT_S + _LOGIN_ALLOWANCE_S

    def snow(sql: str) -> tuple[list[dict] | None, str]:
        """(rows, "") on success, (None, masked error) when ``snow`` exits non-zero."""
        try:
            proc = run(
                ["snow", *_SNOW_ARGS],
                input=sql + ";\n",
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise CiKeyError(_SNOW_MISSING) from None
        except subprocess.TimeoutExpired:
            raise CiKeyError(f"`snow sql` did not finish within {timeout}s.") from None
        except OSError as exc:  # found but not launchable (permissions, a bad binary)
            raise CiKeyError(f"`snow` could not be started ({exc.strerror}).") from None
        if proc.returncode != 0:
            # Redact before and after: _error_detail keeps only the last 600
            # characters, which could otherwise cut a value and leave part of it.
            err = _redact(proc.stderr or "", secret_text)
            out = _redact(proc.stdout or "", secret_text)
            detail = _redact(_error_detail(err, out), secret_text)
            return None, _safe_detail(detail, payloads)
        try:
            return parse_output(proc.stdout or "", 1)[0], ""
        except SnowError as exc:
            raise CiKeyError(_safe_detail(_redact(str(exc), secret_text), payloads)) from None

    schemas = [(o.app_database, o.app_schema)]
    if cfg.deploy.source == "stage-copy":
        schemas.append((o.stage_database, o.stage_schema))
    schemas = list(dict.fromkeys(schemas))
    probes: list[Probe] = []

    # 1. Sign-in and role.
    rows, err = snow("SELECT CURRENT_ROLE() AS ROLE")
    if rows is None:
        reason = "not run: the sign-in failed"
        probes.append(Probe("role", ci, "fail", f"sign-in or query failed: {err}"))
        probes.append(Probe("warehouse", o.default_warehouse, "skipped", reason))
        probes += [Probe("schema", f"{d}.{s}", "skipped", reason) for d, s in schemas]
        probes.append(Probe("grant", f"grants to role {ci}", "skipped", reason))
        probes.append(Probe("select", obj or "", "skipped", reason))
        return VerifyResult(probes)
    current = str(first_row(rows).get("ROLE") or "")
    if current.upper() == ci.upper():
        probes.append(Probe("role", ci, "pass", "signed in; CURRENT_ROLE() is the CI role"))
    else:
        probes.append(Probe("role", ci, "fail", f"CURRENT_ROLE() is {current or 'NULL'}"))

    # 2. Warehouse.
    rows, err = snow(f"USE WAREHOUSE {o.default_warehouse}")
    probes.append(Probe("warehouse", o.default_warehouse, "fail" if rows is None else "pass", err))

    # 3. The schemas the deploy writes to.
    for db, schema in schemas:
        name = f"{db}.{schema}"
        rows, err = snow(f"SHOW SCHEMAS LIKE {quote_sql_literal(schema)} IN DATABASE {db}")
        if rows is None:
            probes.append(Probe("schema", name, "fail", err))
        elif any(str(r.get("NAME", "")).upper() == schema.upper() for r in upper_rows(rows)):
            probes.append(Probe("schema", name, "pass"))
        else:
            probes.append(Probe("schema", name, "fail", "not visible to the CI role"))

    # 4. Grants, against the admin script.
    rows, err = snow(f"SHOW GRANTS TO ROLE {ci}")
    if rows is None:
        probes.append(Probe("grant", f"grants to role {ci}", "fail", err))
    else:
        held = {
            (
                str(r.get("PRIVILEGE", "")).upper(),
                str(r.get("GRANTED_ON", "")).upper().replace("_", " "),
                str(r.get("NAME", "")).replace('"', "").upper(),
            )
            for r in upper_rows(rows)
        }
        for g in grants:
            # OWNERSHIP implies every privilege on the object. A shared database's
            # IMPORTED PRIVILEGES may be listed as USAGE (not yet verified live).
            accepted = {g.privilege.upper(), "OWNERSHIP"}
            if g.privilege.upper() == "IMPORTED PRIVILEGES":
                accepted.add("USAGE")
            key = (g.granted_on.upper(), g.name.upper())
            ok = any((p, *key) in held for p in accepted)
            label = f"{g.privilege} on {g.granted_on} {g.name}"
            probes.append(Probe("grant", label, "pass" if ok else "fail", "" if ok else "missing"))

    # 5. A read of one allowlisted object.
    if select_sql is None:
        probes.append(
            Probe("select", "", "skipped", "pass --object DB.SCHEMA.OBJECT to test a read")
        )
    else:
        rows, err = snow(select_sql)
        probes.append(Probe("select", obj or "", "fail" if rows is None else "pass", err))
    return VerifyResult(probes)
