"""Read-only check that each governance source is visible, for configure and doctor --live.

Why this exists
---------------
A source name is typed by a person, often from memory, and a wrong one is
silent until the deployed dashboard comes up empty: deploy-setup grants on a
schema that does not exist (or exists in another database), and schema-refs
can only check names against the config, not against Snowflake. One login with
SHOW statements catches the typo while the user is still at the prompt.

What it proves, and what it does not
------------------------------------
``SHOW TERSE SCHEMAS LIKE '<schema>' IN ACCOUNT`` lists the schemas the
current role holds any privilege on, across databases, with their
``database_name``. So ``visible`` means "this role can see the schema", not
"this role can SELECT from it"; and the role is the connection's own (secondary
roles off, through ``sf_exec``), never the CI role the deployed app reads as.
The report names that role so nobody mistakes one for the other. ``SHOW
DATABASES`` adds each database's ``kind``: an ``IMPORTED DATABASE`` holding a
source needs IMPORTED PRIVILEGES, which configure records in
``governance.imported_databases``.

Catalog names are compared exactly as Snowflake stores them. A config entry is
an unquoted identifier, which Snowflake stores upper-case, while ``LIKE`` in
SHOW matches case-insensitively; so a schema created as ``"reporting"``
(quoted, lower case) comes back from the SHOW but is a different schema, and
must not count as ``REPORTING``. This assumes Snowflake's default
``QUOTED_IDENTIFIERS_IGNORE_CASE = FALSE``; an account that sets it TRUE
resolves such a name to ``REPORTING``, and the probe then reports it as not
visible, a false warning rather than a false pass.

Degrade, don't die (principle 6): no connection, no ``snow``, a failed login or
an output shape this code does not recognize make the targets ``unverified``
with the reason. A probe never fails configure or doctor.

Docs: https://docs.snowflake.com/en/sql-reference/sql/show-schemas and
https://docs.snowflake.com/en/sql-reference/sql/show-databases
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import sf_exec as sx
from .config import ConfigError, quote_sql_literal, validate_schema_ref

VISIBLE = "visible"
NOT_VISIBLE = "not_visible"
UNVERIFIED = "unverified"
_ROLE_SQL = "SELECT CURRENT_ROLE() AS ROLE"


@dataclass(frozen=True)
class ProbeResult:
    target: str  # DATABASE.SCHEMA, upper-cased
    status: str  # visible | not_visible | unverified
    role: str | None  # the role the probe ran as; None when nothing ran
    detail: str = ""


@dataclass(frozen=True)
class ProbeReport:
    role: str | None
    results: tuple[ProbeResult, ...]
    imported_databases: tuple[str, ...] = ()  # every IMPORTED DATABASE the role can see
    error: str = ""  # why every result is unverified, when one call failed


def unverified(targets: Sequence[str], why: str) -> ProbeReport:
    """Every target unverified for one reason (no connection, no snow, a failed login)."""
    return ProbeReport(
        role=None,
        results=tuple(ProbeResult(str(t).strip().upper(), UNVERIFIED, None, why) for t in targets),
        error=why,
    )


def probe_schemas(
    connection: str | None,
    targets: Sequence[str],
    *,
    role: str | None = None,
    runner: sx.Runner | None = None,
    timeout_s: int = 60,
    max_wait_s: int | None = None,
) -> ProbeReport:
    """Probe every ``DATABASE.SCHEMA`` in *targets* in one ``snow sql`` call.

    ``max_wait_s`` caps the whole call, login included. ``SnowExec`` budgets
    ``timeout_s`` per statement plus a 120 s login allowance, which is right for doctor
    but too long for configure: a missed SSO window would block the wizard past an agent
    command's own timeout, before the config is written. With a cap, a wait that runs
    out is ``unverified`` (with the reason), never an exception.
    """
    if not connection:
        return unverified(targets, "no snow connection to probe with")
    try:
        wanted = [validate_schema_ref(t, "source") for t in targets]
        session = sx.Session(
            connection=connection, role=role, query_tag="streamsnow:probe", timeout_s=timeout_s
        )
        inner = runner or sx._default_runner
        capped = (
            inner
            if max_wait_s is None
            else lambda argv, stdin, timeout: inner(argv, stdin, min(timeout, max_wait_s))
        )
        ex = sx.SnowExec(session, None, runner=capped)
        names = sorted({t.split(".", 1)[1] for t in wanted})
        statements = [
            _ROLE_SQL,
            "SHOW DATABASES",
            *(f"SHOW TERSE SCHEMAS LIKE {quote_sql_literal(n)} IN ACCOUNT" for n in names),
        ]
        results = ex.run(statements)
    except sx.SnowTimeout as exc:
        if max_wait_s is None:
            return unverified(targets, str(exc))
        return unverified(
            targets,
            f"no answer from Snowflake within {max_wait_s}s (a sign-in window left open counts); "
            "`streamsnow doctor --live` checks the sources later",
        )
    except (sx.SnowError, ConfigError) as exc:
        return unverified(targets, str(exc))
    ran_as = str(sx.first_row(results[0]).get("ROLE") or "") or None
    imported = tuple(
        sorted(
            {
                str(r.get("NAME", ""))
                for r in sx.upper_rows(results[1])
                if str(r.get("KIND", "")).upper() == "IMPORTED DATABASE"
            }
        )
    )
    seen: set[tuple[str, str]] = set()
    shapeless: set[str] = set()
    for name, rows in zip(names, results[2:], strict=True):
        for r in sx.upper_rows(rows):  # upper-cases the KEYS only; values stay as stored
            if "DATABASE_NAME" not in r:
                shapeless.add(name)
                continue
            seen.add((str(r["DATABASE_NAME"]), str(r.get("NAME", ""))))
    out: list[ProbeResult] = []
    for target in wanted:
        db, schema = target.split(".", 1)
        if (db, schema) in seen:
            out.append(ProbeResult(target, VISIBLE, ran_as))
        elif schema in shapeless:
            out.append(
                ProbeResult(target, UNVERIFIED, ran_as, "SHOW SCHEMAS printed no database_name")
            )
        else:
            out.append(ProbeResult(target, NOT_VISIBLE, ran_as, f"not visible to role {ran_as}"))
    return ProbeReport(ran_as, tuple(out), imported)


def probe_schema(
    connection: str | None,
    db_schema: str,
    *,
    role: str | None = None,
    runner: sx.Runner | None = None,
) -> ProbeResult:
    """One target: :func:`probe_schemas` for a single ``DATABASE.SCHEMA``."""
    return probe_schemas(connection, [db_schema], role=role, runner=runner).results[0]
