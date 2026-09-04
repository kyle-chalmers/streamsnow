#!/usr/bin/env python3
"""Generate and verify ``sql_review/`` — human-runnable proof for every visual.

Why this exists
---------------
Apps store their queries as templates under ``apps/<slug>/queries/*.sql`` and
render them at runtime with ``{TOKEN}`` fragments and ``:N`` bind params. A
reviewer reading the raw templates cannot see what actually hits Snowflake —
and a dashboard whose numbers nobody can independently re-run is a dashboard
nobody can sign off. So every produced or altered page leaves behind an
**audit trail**: fully-rendered, paste-and-runnable review SQL a person opens
in Snowsight to trace each visual back to the data and confirm it.

Generation is deterministic (this tool), the lineage narrative is
Claude-assisted (the ``/review-app --sql`` recipe), and freshness is enforced
(the ``check`` verb, wired into pre-commit / CI / the validate gate).

Directory contract (per app)
----------------------------
::

    apps/<slug>/sql_review/
      README.md                      # index: query → upstream → feeds → file → verified?
      manifests/<feature>.json       # co-located with the app it describes
      <feature>[.<combo>].review.sql # paste-runnable in Snowsight

Manifests live **inside the app** on purpose: renaming or retiring an app
moves its whole audit trail with it, and a consumer repo has no central
tools/ directory to host them.

Manifest schema (v1)
--------------------
::

    {
      "schema_version": 1,
      "feature": "revenue",
      "app": "acme-sales-dashboard",
      "description": "Revenue pages",
      "token_strategy": "static",          // or "manifest"
      "modules": {"data": "data"},          // manifest strategy only
      "token_dispatchers": {
        "REGION_FILTER": {"literal": "AND region = 'REPLACE_WITH_REGION'"},
        "CHANNEL_FILTER": {"call": "channel_filter_sql", "args": ["@combo.channel"]}
      },
      "combos": [{"name": "all-default", "description": "no filters", "channel": "All"}],
      "param_bindings": {"1": "$start_date", "2": "$end_date"},
      "set_block": {"start_date": "DATEADD('year', -1, CURRENT_DATE)",
                     "end_date": "CURRENT_DATE"},
      "set_block_note": "why these defaults — which source the bounds derive
                          from, and any mechanics that bite when editing them",
      "fragments": [{"file": "_shared_ctes.sql",
                      "reason": "inlined via {SHARED_CTES}; not runnable alone"}],
      "pages": [{"name": "Overview", "queries": ["revenue_summary"]}],
      "query_specs": {
        "revenue_summary": {"tokens": ["REGION_FILTER"],
                             "params_doc": ":1 start_date, :2 end_date",
                             "metric_name": "revenue_by_day"}
      }
    }

Two token strategies:

- ``static`` (default; the no-import path): every dispatcher must be a
  ``literal`` — tokens resolve from the manifest alone. ``discover`` always
  proposes this shape.
- ``manifest``: ``call`` / ``const_attr`` dispatchers resolve against the
  app's own modules (``modules`` maps import names), so the rendered SQL is
  exactly what the app emits. **Only ``generate`` may import consumer app
  code**, and only on a developer's machine.

Metrics mode (``"mode": "metrics"``)
------------------------------------
For dashboards whose visuals aggregate differently than any single app query:
one AUTHORED block per visual, in on-screen order, from files under
``sql_review/_metrics/*.sql`` (or a ``queries/*.sql`` when a visual is 1:1
with an app query — that reference also claims the query for coverage). No
combos, no dispatchers, never any import; a dashboard-map index heads the
single ``<feature>.review.sql``. Sources are allowlisted to the two roots
(traversal-safe) and digest-pinned like every other input.

Import-free ``check`` (the CI / pre-commit / validate hook)
-----------------------------------------------------------
``check`` NEVER imports app code — importing consumer modules from a shared
hook would execute arbitrary code on every commit. Instead each generated
file carries a provenance line::

    -- Provenance: schema=1 inputs=<sha256/16> output=<sha256/16>

``inputs`` digests the manifest bytes, every referenced ``queries/*.sql``
template, and (manifest strategy) the app module files the dispatchers call
into. ``output`` digests the rendered file itself with the volatile lines
(Generated date, the provenance line) normalized. ``check`` recomputes both
hashes statically: an edited template, manifest, module, or hand-edited
review file all read as DRIFT. ``check`` also enforces **coverage**: every
``queries/*.sql`` in the app must be claimed by some manifest — a query the
generator can't account for is a hard, named failure, never a silent skip.

Read-only guard
---------------
Rendered output is verified with a statement-root **allowlist** — only
``SELECT`` / ``WITH``-terminating-in-``SELECT`` / ``SHOW`` / ``DESCRIBE`` /
``EXPLAIN`` statements plus ``SET <ident> =`` session-variable assignments may
be emitted (an allowlist, not a write-verb denylist: the failure mode of a
denylist is the statement type nobody thought of). All structural analysis
runs on text with string literals and comments masked, so literal contents
can never influence parsing. A manifest whose rendered output violates this
is an error; nothing is written, and ``check`` re-verifies committed files.

Scope honesty: this guard (and the provenance digests) catches accidents and
drift, and blocks templates from ever emitting a write. It is NOT a proof
against a deliberate committer with write access — the digest algorithm is
public and there is no signing key. Repository review remains the trust
boundary for malicious commits.

Verbs
-----
``discover <slug>``   propose features + skeleton manifests for uncovered
                      queries (JSON to stdout; ``--write`` persists skeletons).
                      Exit 1 = gaps exist.
``generate <slug>``   render review files (+ provenance) from manifests.
``check <slug>``      import-free freshness + coverage gate. Exit 1 = drift
                      or uncovered queries.
``index <slug>``      rebuild the README.md coverage table (tool owns the
                      table; the lineage narrative around it is authored by
                      the review recipe and preserved).

Exit codes: 0 = clean, 1 = findings/drift/gaps, 2 = tool error.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import os
import re
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

#: Bumped when the rendered-file format changes shape — makes every prior
#: file read as drift, which is correct: format changes need a regenerate.
GENERATOR_SCHEMA = 1

#: Statement roots a review file may contain. SET is restricted separately
#: to session-variable assignments (see _verify_read_only).
ALLOWED_ROOTS = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"})

# Prefix form, plus Snowflake's documented multi-variable form
# `SET (a, b) = (expr, expr)`.
_SET_STMT_RE = re.compile(r"^SET\s+(?:[A-Za-z_][A-Za-z0-9_$]*|\([^)]*\))\s*=", re.IGNORECASE)
_PROVENANCE_RE = re.compile(
    r"^-- Provenance: schema=(\d+) inputs=([0-9a-f]{16}) output=([0-9a-f]{16})\s*$",
    re.MULTILINE,
)
_GENERATED_RE = re.compile(r"^-- Generated: \d{4}-\d{2}-\d{2} by streamsnow sql-review$")

_HEADER_FIELD_RE = re.compile(r"^--\s*(Query|Feeds|Schemas|Params|Tokens):\s*(.*)$")
_TOKEN_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
# A bind marker is `:N` in an operand position, so it never directly follows an
# identifier character. Requiring that excludes `::` casts AND Snowflake
# semi-structured access with a numeric key (`payload:1`), which would otherwise
# read as a surviving bind and refuse to generate a perfectly valid file.
_BIND_RE = re.compile(r"(?<![:\w\"$]):(\d+)\b")

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_FEATURE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ToolError(RuntimeError):
    """Cannot proceed — reported on stderr with exit 2."""


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _app_dir(repo: Path, slug: str) -> Path:
    if not _SLUG_RE.match(slug):
        raise ToolError(f"app slug {slug!r} must be kebab-case (^[a-z][a-z0-9-]*$)")
    app = repo / "apps" / slug
    if not app.is_dir():
        raise ToolError(f"no app at {app}")
    return app


def _review_dir(app: Path) -> Path:
    return app / "sql_review"


def _manifest_paths(app: Path) -> list[Path]:
    mdir = _review_dir(app) / "manifests"
    return sorted(mdir.glob("*.json")) if mdir.is_dir() else []


def _query_files(app: Path) -> list[Path]:
    qdir = app / "queries"
    return sorted(qdir.glob("*.sql")) if qdir.is_dir() else []


# --------------------------------------------------------------------------- #
# Query-template parsing
# --------------------------------------------------------------------------- #
def parse_header(text: str) -> dict[str, str]:
    """The leading ``-- Field: value`` block of a query template."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("--"):
            break
        m = _HEADER_FIELD_RE.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def strip_header(text: str) -> str:
    """Drop the leading comment block so rendered files don't duplicate it."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith("--") or not lines[i].strip()):
        i += 1
    return "\n".join(lines[i:])


def template_tokens(text: str) -> list[str]:
    """Distinct ``{TOKEN}`` placeholders in body order (header lines excluded —
    the ``-- Tokens:`` documentation line names tokens without braces for
    exactly this reason, but be safe about stray commented examples)."""
    seen: list[str] = []
    for line in strip_header(text).splitlines():
        if line.lstrip().startswith("--"):
            continue
        for m in _TOKEN_RE.finditer(line):
            if m.group(1) not in seen:
                seen.append(m.group(1))
    return seen


# --------------------------------------------------------------------------- #
# Manifest loading + validation
# --------------------------------------------------------------------------- #
def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"{path}: unreadable manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolError(f"{path}: manifest root must be an object")
    problems = validate_manifest(data)
    if problems:
        raise ToolError(f"{path}: invalid manifest: " + "; ".join(problems))
    return data


#: Metric source paths may live in exactly these app-relative roots. The
#: allowlist (not a "no .." check alone) is what makes the path traversal-safe:
#: a source is read and hashed, so it must never reach outside the app.
_METRIC_SOURCE_RE = re.compile(r"^(sql_review/_metrics|queries)/[A-Za-z0-9_][A-Za-z0-9_.-]*\.sql$")


def _validate_metrics_manifest(m: dict, out: list[str]) -> None:
    """Extra rules for ``mode: "metrics"`` — per-visual authored blocks.

    Metrics mode carries no combos/pages/dispatchers: each entry is one
    dashboard visual, in on-screen order, whose SQL is an authored file. The
    named ``source`` is both read and digest-pinned, so it is allowlisted to
    the two sanctioned roots.
    """
    metrics = m.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        out.append("mode 'metrics' requires a non-empty metrics[] list")
        return
    for i, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            out.append(f"metrics[{i}] must be an object")
            continue
        for req in ("name", "page", "title", "source"):
            if not metric.get(req):
                out.append(f"metrics[{i}] needs {req!r}")
        source = str(metric.get("source", ""))
        if source and not _METRIC_SOURCE_RE.match(source):
            out.append(
                f"metrics[{i}].source {source!r} must be an app-relative path under "
                "sql_review/_metrics/ or queries/ (resolved strictly inside the app)"
            )


def _validate_fragments(m: dict, out: list[str]) -> None:
    """Validate ``fragments`` entries.

    A fragment declaration SUPPRESSES a coverage requirement, so a malformed
    one must be an error rather than silently ignored — otherwise the shape
    that looks like it works (a bare list of strings) quietly grants no
    exemption, and a path-shaped one grants the WRONG exemption:
    ``../../x.sql`` and ``sub/dir/x.sql`` both reduce to the stem ``x`` and
    would exempt ``queries/x.sql``. Anything that can turn the gate off is
    validated strictly and must name exactly one file in ``queries/``.
    """
    frags = m.get("fragments")
    if frags is None:
        return
    if not isinstance(frags, list):
        out.append("fragments must be a list of {file, reason} objects")
        return
    seen: set[str] = set()
    for idx, entry in enumerate(frags):
        where = f"fragments[{idx}]"
        if not isinstance(entry, dict):
            out.append(
                f"{where} must be an object like "
                '{"file": "_shared_ctes.sql", "reason": "…"} '
                f"(got {type(entry).__name__})"
            )
            continue
        fname = entry.get("file")
        if not isinstance(fname, str) or not fname:
            out.append(f"{where}.file is required and must be a string")
            continue
        if "/" in fname or "\\" in fname or fname in (".", "..") or fname.startswith("."):
            out.append(
                f"{where}.file {fname!r} must be a bare filename in queries/ — "
                "no path separators and no traversal (a path would exempt a "
                "different file than it appears to name)"
            )
            continue
        if not fname.endswith(".sql"):
            out.append(f"{where}.file {fname!r} must end in .sql")
            continue
        if not str(entry.get("reason", "")).strip():
            out.append(
                f"{where}.reason is required — a coverage exemption must record WHY "
                "the file has no runnable companion"
            )
        stem = fname[: -len(".sql")]
        if stem in seen:
            out.append(f"{where}.file {fname!r} declared more than once")
        seen.add(stem)
        try:
            claimed = _referenced_queries(m)
        except (AttributeError, TypeError):
            # `pages` is malformed; the page validators below report that
            # properly. Bailing here keeps a shape error from surfacing as a
            # traceback just because a valid fragment happened to be declared.
            claimed = set()
        if stem in claimed:
            out.append(
                f"{where}.file {fname!r} is also claimed by a page — a query is either "
                "a runnable query or an inlined fragment, not both"
            )


def validate_manifest(m: dict) -> list[str]:
    """Schema-validate one manifest dict. Returns problem strings (empty = ok)."""
    out: list[str] = []
    if m.get("schema_version") != 1:
        out.append(f"schema_version must be 1 (got {m.get('schema_version')!r})")
    feature = m.get("feature", "")
    if not isinstance(feature, str) or not _FEATURE_RE.match(feature):
        out.append(f"feature {feature!r} must match ^[a-z][a-z0-9_-]*$")
    mode = m.get("mode", "tokens")
    if mode not in ("tokens", "metrics"):
        out.append(f"mode must be 'tokens' (default) or 'metrics' (got {mode!r})")
    sb = m.get("set_block")
    if sb is not None:
        if not isinstance(sb, dict):
            out.append("set_block must be an object of {variable: expression}")
        else:
            for k, v in sb.items():
                if not isinstance(v, str) or not v.strip():
                    out.append(
                        f"set_block[{k!r}] must be a non-empty SQL expression — an empty "
                        "value renders `SET " + str(k) + " = ;`, which is invalid SQL that "
                        "still looks like a definition to the session-variable check"
                    )
    note = m.get("set_block_note")
    if note is not None and not isinstance(note, str):
        out.append(f"set_block_note must be a string (got {type(note).__name__})")
    _validate_fragments(m, out)
    if mode == "metrics":
        _validate_metrics_manifest(m, out)
        return out
    strategy = m.get("token_strategy", "static")
    if strategy not in ("static", "manifest"):
        out.append(f"token_strategy must be 'static' or 'manifest' (got {strategy!r})")
    combos = m.get("combos", [{"name": "all-default", "description": "defaults"}])
    if not isinstance(combos, list) or not combos:
        out.append("combos must be a non-empty list")
    else:
        for c in combos:
            if not isinstance(c, dict) or not c.get("name"):
                out.append("every combo needs a name")
                break
            # Combo names land in output FILENAMES — restrict to a safe
            # basename charset so a manifest can never write outside
            # sql_review/ (path traversal via "name": "../../x").
            if not re.match(r"^[a-z0-9][a-z0-9_-]*$", str(c["name"])):
                out.append(
                    f"combo name {c['name']!r} must match ^[a-z0-9][a-z0-9_-]*$ "
                    "(it becomes part of the output filename)"
                )
                break
    for q, spec in (m.get("query_specs") or {}).items():
        source = (spec or {}).get("source_query", q)
        # source_query resolves to queries/<name>.sql — same traversal risk.
        if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$", str(source)):
            out.append(
                f"query_specs[{q!r}].source_query {source!r} must be a bare query name "
                "(resolved strictly inside queries/)"
            )
    pages = m.get("pages")
    if not isinstance(pages, list) or not pages:
        out.append("pages must be a non-empty list of {name, queries}")
    else:
        for p in pages:
            if (
                not isinstance(p, dict)
                or not p.get("name")
                or not isinstance(p.get("queries"), list)
            ):
                out.append("every pages[] entry needs name + queries[]")
                break
            for q in p["queries"]:
                # Query names resolve to queries/<name>.sql when no spec
                # overrides source_query — bare names only, no traversal.
                if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$", str(q)):
                    out.append(f"pages[].queries entry {q!r} must be a bare query name")
                    break
    dispatchers = m.get("token_dispatchers", {})
    if strategy == "static":
        for tok, d in dispatchers.items():
            if not isinstance(d, dict) or "literal" not in d:
                out.append(
                    f"token {tok!r}: static strategy requires a 'literal' dispatcher "
                    "(call/const_attr need token_strategy 'manifest')"
                )
    return out


# --------------------------------------------------------------------------- #
# Token resolution
# --------------------------------------------------------------------------- #
def _resolve_arg(arg: object, combo: dict) -> object:
    if isinstance(arg, str) and arg.startswith("@combo."):
        key = arg[len("@combo.") :]
        if key not in combo:
            raise ToolError(f"combo key {key!r} referenced by a dispatcher but absent")
        return combo[key]
    return arg


def _resolve_token(tok: str, dispatcher: dict, combo: dict, modules: dict) -> str:
    if "literal" in dispatcher:
        return str(_resolve_arg(dispatcher["literal"], combo))
    if "const_attr" in dispatcher:
        module = modules.get(dispatcher.get("module", "data"))
        if module is None:
            raise ToolError(f"token {tok!r}: const_attr needs an imported module")
        return str(getattr(module, dispatcher["const_attr"]))
    if "call" in dispatcher:
        module = modules.get(dispatcher.get("module", "data"))
        if module is None:
            raise ToolError(f"token {tok!r}: call needs an imported module")
        fn = getattr(module, dispatcher["call"])
        args = [_resolve_arg(a, combo) for a in dispatcher.get("args", [])]
        return str(fn(*args))
    raise ToolError(f"dispatcher for token {tok!r} has none of literal/const_attr/call")


def _dispatcher_banner(tok: str, dispatcher: dict, combo: dict) -> str:
    """Human-readable intent line for the section banner."""
    if "literal" in dispatcher:
        ref = dispatcher["literal"]
        if isinstance(ref, str) and ref.startswith("@combo."):
            key = ref[len("@combo.") :]
            return f"{tok} = {key}={combo.get(key)!r}"
        return f"{tok} = {ref!r}"
    if "const_attr" in dispatcher:
        return f"{tok} = (const: {dispatcher['const_attr']})"
    if "call" in dispatcher:
        parts = []
        for a in dispatcher.get("args", []):
            if isinstance(a, str) and a.startswith("@combo."):
                key = a[len("@combo.") :]
                parts.append(f"{key}={combo.get(key)!r}")
            else:
                parts.append(repr(a))
        return f"{tok} = {dispatcher['call']}({', '.join(parts)})"
    return f"{tok} = ?"


def _import_modules(app: Path, manifest: dict) -> dict:
    """Import the app modules the ``manifest`` strategy dispatchers call into.

    ONLY ``generate`` reaches here, only for token_strategy 'manifest', and
    only on a developer machine — see the module docstring. Streamlit's
    "no runtime" warnings on import are suppressed.
    """
    modules: dict[str, object] = {}
    names = manifest.get("modules") or {"data": "data"}
    sys.path.insert(0, str(app))
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            for alias, mod_name in names.items():
                if mod_name is None:
                    continue
                modules[alias] = importlib.import_module(mod_name)
    except Exception as exc:  # noqa: BLE001 — surface as a tool error, not a traceback
        raise ToolError(
            f"failed to import app modules for token_strategy 'manifest' ({exc}); "
            "run from an environment with the app's dependencies installed"
        ) from exc
    finally:
        sys.path.pop(0)
    return modules


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_DEFAULT_BINDS = {"1": "$start_date", "2": "$end_date"}
_DEFAULT_SET = {
    "start_date": "DATEADD('year', -1, CURRENT_DATE)",
    "end_date": "CURRENT_DATE",
}


def _substitute_binds(sql: str, binds: dict[str, str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        return binds.get(match.group(1), match.group(0))

    return _BIND_RE.sub(_sub, sql)


def _banner(title: str, sublines: list[str]) -> str:
    rule = "-- " + "=" * 69
    soft = "-- " + "-" * 69
    body = [rule, f"-- {title}", soft]
    body.extend(f"-- {line}" for line in sublines)
    body.append(rule)
    return "\n".join(body)


def _var_used(name: str, body: str) -> bool:
    """Is session variable ``name`` actually referenced in the rendered body?

    Word-boundary anchored so ``$start_date`` is not matched by
    ``$start_date_x``, and CASE-INSENSITIVE because Snowflake identifiers are:
    ``$START_DATE`` and ``$start_date`` are the same variable. Matching
    case-sensitively here would prune a SET line that IS used, leaving a
    dangling reference that errors on paste — a worse failure than the unused
    SET line this pruning exists to remove. So it fails toward keeping: an
    extra SET line is harmless, a missing one breaks the file.
    """
    # Masked, so a `$name` occurring inside a string literal or a quoted
    # identifier (`SELECT "$start_date"`) does not count as a use — otherwise a
    # SET line is kept and the header promises a reference that is only text.
    return (
        re.search(
            r"\$" + re.escape(name) + r"\b",
            _mask_strings_and_comments(body),
            re.IGNORECASE,
        )
        is not None
    )


def _set_block(manifest: dict, body: str | None = None) -> str:
    """Render the SET block, pruned to the variables the body actually uses.

    ``body`` is the already-rendered query text. Passing it prunes SET lines for
    variables nothing references and returns "" when none survive. This is not
    cosmetic: a SET block whose variables are unused, under a header promising
    "edit the SET lines to change the review window", makes a reviewer edit the
    window, rerun, get byte-identical numbers, and conclude the data is
    window-stable when the window was never applied. A confidently wrong
    verification is worse than no SET block at all. Some queries self-anchor
    internally (CURRENT_DATE / DATE_TRUNC / DATEADD) and take no date binds.
    """
    pairs = manifest.get("set_block") or _DEFAULT_SET
    emitted: list[str] = []
    for name, expr in pairs.items():
        if body is not None and not _var_used(name, body):
            continue
        emitted.append(f"SET {name} = {expr};")
    for sv in manifest.get("set_vars", []):
        if body is not None and not _var_used(sv["name"], body):
            continue
        if sv.get("comment"):
            emitted.append(f"-- {sv['comment']}")
        emitted.append(f"SET {sv['name']} = {sv['default']};")
    if not emitted:
        return ""
    intro = [
        "-- Edit these SET lines to change the review parameters. Every section below",
        "-- references the session variables — no per-section edits required.",
    ]
    # `set_block_note` carries WHY these defaults are what they are — which
    # source the bounds derive from, why that source and not the calendar, and
    # any mechanics that bite when editing them. That rationale is the
    # difference between an auditor reproducing the page and an auditor
    # reproducing a number that merely looks plausible, so it renders inline
    # rather than living in a manifest nobody opens.
    raw_note = manifest.get("set_block_note") or ""
    note = raw_note.strip() if isinstance(raw_note, str) else ""
    if note:
        intro += [f"-- {line}" for line in textwrap.wrap(note, width=94)]
    return "\n".join([*intro, *emitted])


def _bind_note(has_set_block: bool) -> list[str]:
    """Header lines describing bind handling — must match what was emitted."""
    if has_set_block:
        return [
            "Bind params are replaced with session variables (see the SET block);",
            "edit the SET lines once to apply new values across every section.",
        ]
    # Claim ONLY what emitting no SET block actually proves: no bind params and
    # no session variables. HOW each section bounds itself is a property of the
    # query, which this function never inspected — asserting "self-anchors on
    # CURRENT_DATE / DATE_TRUNC / DATEADD" is the same species of misdescription
    # the pruning exists to remove (the body might use hardcoded literal dates).
    return [
        "No SET block: no section below takes a bind param or references a session",
        "variable, so there is no shared review window to edit here. Each section",
        "bounds its own range — read the query to see how, and edit it to change it.",
    ]


def _mask_strings_and_comments(text: str) -> str:
    """Replace string-literal contents and comments with spaces, same length.

    Every structural decision downstream (statement splitting, paren
    balancing, verb extraction) runs on the MASKED text — a ``)`` or ``;`` or
    verb-shaped word inside a string literal must never influence structure.
    This closed two real bypasses:

    * ``WITH x AS (SELECT ')SELECT' …) DELETE …`` — a single-quoted literal
      fooled a raw paren counter into reading the literal's contents as the
      terminal verb.
    * ``WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t`` — a DOUBLE-quoted
      delimited identifier did the same thing. Snowflake treats ``"…"`` as an
      identifier, not a string, so it was initially left unmasked; the ``)``
      inside it closed the CTE scan early and the trailing ``SELECT`` was read
      as the terminal verb while Snowflake executed the ``DELETE``.

    * ``WITH x AS (SELECT $$ ) SELECT y $$) DELETE FROM t`` — a dollar-quoted
      constant, which was not recognised as a quoting form at all.
    * ``WITH x AS (SELECT '\\') SELECT y') DELETE FROM t`` — a BACKSLASH-escaped
      quote. Snowflake accepts both ``''`` and ``\'``; only the doubling form
      was handled, so the literal looked closed at the wrong place.

    All of these must therefore be masked, for structure only — this
    function's output is never emitted, so losing identifier text is fine.
    Handles ``''`` / ``""`` escaping; an unterminated literal masks to
    end-of-text, which downstream reads as "cannot parse" → not allowed.
    Length and newlines are preserved so nothing shifts.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "$" and text[i : i + 2] == "$$":  # dollar-quoted constant
            end = text.find("$$", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
        elif c in "'\"":  # string literal, or double-quoted delimited identifier
            quote = c
            i += 1
            while i < n:
                # Snowflake accepts BOTH doubling ('' / "") and backslash
                # escaping (\') inside a string literal. Missing the backslash
                # form let `'\') SELECT y'` read as a closed literal, so the
                # `)` escaped masking and ended the CTE scan early. Backslash
                # is not an escape inside a double-quoted identifier, so this
                # only applies to string literals.
                if quote == "'" and text[i] == "\\" and i + 1 < n:
                    for j in (i, i + 1):
                        if text[j] != "\n":
                            out[j] = " "
                    i += 2
                    continue
                if text[i] == quote and i + 1 < n and text[i + 1] == quote:
                    out[i] = out[i + 1] = " "  # escaped quote ('' or "")
                    i += 2
                    continue
                if text[i] == quote:
                    break
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            i += 1
        elif c == "-" and text[i : i + 2] == "--":  # line comment
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and text[i : i + 2] == "/*":  # block comment
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
        else:
            i += 1
    return "".join(out)


def _verify_session_vars_defined(text: str) -> list[str]:
    """Every ``$var`` the body references must have a ``SET`` line in the file.

    The symmetric half of the SET-block pruning. Pruning removes SET lines for
    DECLARED variables the body never references; nothing checked the converse,
    that every variable the body DOES reference was declared. A manifest whose
    ``param_bindings`` name a variable absent from ``set_block`` therefore
    rendered binds into undefined session variables, pruned the SET block to
    empty because neither declared name was referenced, and emitted a header
    stating "no section below takes a bind param or references a session
    variable" — with generate returning 0 and check reporting clean.

    Pasted into Snowsight that file dies on the first block with "Session
    variable '$WINDOW_START' does not exist". A confidently wrong verification
    is the exact failure this release exists to remove, so the check runs over
    the final text: it catches an undeclared variable and any future pruning
    mistake alike, without trusting the manifest.
    """
    masked = _mask_strings_and_comments(text)
    defined = {m.group(1).lower() for m in re.finditer(r"(?mi)^SET\s+([A-Za-z_]\w*)\s*=", masked)}
    # Snowflake's documented multi-variable form: `SET (a, b) = (expr, expr)`.
    # Without this both names read as undefined and a valid hand-edited file
    # was falsely refused.
    for m in re.finditer(r"(?mi)^SET\s+\(([^)]*)\)\s*=", masked):
        defined |= {n.strip().lower() for n in m.group(1).split(",") if n.strip()}
    problems: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\$([A-Za-z_]\w*)\b", masked):
        name = m.group(1)
        key = name.lower()
        if key in defined or key in seen:
            continue
        seen.add(key)
        problems.append(
            f"session variable ${name} is referenced but never SET — declare it in the "
            "manifest's set_block (param_bindings must point at a declared variable)"
        )
    return problems


def _verify_binds_bound(text: str) -> list[str]:
    """Every ``:N`` bind must have been substituted in executable lines.

    A surviving ``:N`` is not valid Snowflake outside a driver-bound statement,
    so the block errors the moment it is pasted — the exact failure the whole
    artifact exists to avoid. This is the assertion that was missing when a
    manifest declaring only ``:1``/``:2`` rendered seven live ``AND col <= :3``
    predicates: the read-only allowlist passed it, the provenance hashes
    passed it, and coverage passed it, because none of them ask whether the
    output actually runs.

    Comments are masked first, so the ``Params: :1 start_date`` banner lines
    that document the original slots are exempt by construction. ``::`` casts
    are already excluded by ``_BIND_RE``.
    """
    problems: list[str] = []
    masked = _mask_strings_and_comments(text)
    for lineno, line in enumerate(masked.splitlines(), start=1):
        for m in _BIND_RE.finditer(line):
            problems.append(
                f"line {lineno}: unsubstituted bind :{m.group(1)} — declare it in the "
                "manifest's param_bindings (and set_block) so it renders a value"
            )
    return problems


def _split_statements(text: str) -> list[str]:
    """Split masked text on ``;``. Comments and string contents are already
    spaces (see ``_mask_strings_and_comments``), so a ``;`` in a literal or a
    block comment can neither split a legitimate statement nor hide one."""
    masked = _mask_strings_and_comments(text)
    return [s.strip() for s in masked.split(";") if s.strip()]


def _with_terminal_verb(stmt: str) -> str:
    """The top-level verb a ``WITH`` statement terminates in, or "".

    A CTE prefix is not read-only by itself — ``WITH x AS (SELECT 1) DELETE
    FROM t`` is a delete. Walk ``name [(cols)] AS ( … )`` definitions with
    paren balancing; whatever keyword follows the last CTE is the statement's
    real verb. Anything this walker cannot confidently parse returns "" and
    fails toward "not allowed".

    Callers MUST pass masked text (``_mask_strings_and_comments``): the paren
    balance is only sound when string-literal contents cannot contribute
    parens or verb-shaped words.
    """
    tokens = stmt.split()
    if not tokens or tokens[0].upper() != "WITH":
        return ""
    # Re-scan character-wise for balanced parens; token-wise is not enough
    # because CTE bodies contain arbitrary whitespace/commas.
    i = len("WITH")
    n = len(stmt)
    while True:
        # Skip whitespace, optional RECURSIVE, the CTE name, optional column
        # list, AS, then the balanced parenthesised body.
        while i < n and stmt[i].isspace():
            i += 1
        # The CTE name may be a DELIMITED identifier (`WITH "cte name" AS …`).
        # Masking blanks its contents but keeps the quotes, so accept a quoted
        # run here as well — otherwise the walker bails, returns "", and a
        # perfectly valid read-only CTE is REFUSED. A false positive is a
        # defect too: it blocks generating a legitimate audit file.
        m = re.match(
            r'(?:RECURSIVE\s+)?(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_$]*)',
            stmt[i:],
            re.IGNORECASE,
        )
        if not m:
            return ""
        i += m.end()
        while i < n and stmt[i].isspace():
            i += 1
        if i < n and stmt[i] == "(":  # optional column list
            depth = 1
            i += 1
            while i < n and depth:
                depth += stmt[i] == "("
                depth -= stmt[i] == ")"
                i += 1
            while i < n and stmt[i].isspace():
                i += 1
        if stmt[i : i + 2].upper() != "AS":
            return ""
        i += 2
        while i < n and stmt[i].isspace():
            i += 1
        if i >= n or stmt[i] != "(":
            return ""
        depth = 1
        i += 1
        while i < n and depth:
            depth += stmt[i] == "("
            depth -= stmt[i] == ")"
            i += 1
        while i < n and stmt[i].isspace():
            i += 1
        if i < n and stmt[i] == ",":
            i += 1
            continue  # next CTE definition
        m = re.match(r"[A-Za-z]+", stmt[i:])
        return m.group(0).upper() if m else ""


# Second, independent layer under the statement-root allowlist. Four masking
# bypasses have been found in this module (single-quote, double-quote,
# dollar-quote, backslash escape), every one of which worked by making the
# structural parser mis-read where a statement began or ended. A recurring
# class like that says the next parser gap should not also be a pass.
#
# Two anchors, deliberately different:
#
# * At the START of a statement, ANY of these verbs is a command. Nothing legal
#   in a read-only file begins with one.
# * Right after a `)`, only RESERVED words are checked. That is the shape every
#   bypass produced (the verb surfacing after a mis-parsed CTE close), but it is
#   also where a bare column alias lives — `SELECT MAX(d) comment FROM t` is
#   legal Snowflake, and `comment` is a real INFORMATION_SCHEMA column this
#   project's own discovery query selects. A reserved word cannot be a bare
#   alias, so the split keeps the bypass coverage without refusing valid SQL.
#   (`AS <verb>` and `"<verb>"` are always fine — masked or clearly an alias.)
#
# There is no `;` anchor: _split_statements strips `;` before this runs, so one
# would be dead code.
_WRITE_VERBS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "TRUNCATE",
    "DROP",
    "UNDROP",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "COMMENT",
    "COPY",
    "PUT",
    "REMOVE",
    "UNLOAD",
    "CALL",
    "EXECUTE",
    "UNSET",
    "SET",
)
# Snowflake reserved words: cannot appear as a bare (unquoted, un-AS'd) alias.
_WRITE_VERBS_RESERVED = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "SET",
)
_WRITE_VERB_AT_START_RE = re.compile(r"\A\s*(" + "|".join(_WRITE_VERBS) + r")\b", re.IGNORECASE)
# The non-reserved verbs CAN be bare column aliases, so they are matched only in
# their two-token COMMAND form. Without this the after-paren anchor was blind to
# `) MERGE INTO t` and `) TRUNCATE TABLE t`. The allowlist catches those today,
# but this layer exists to hold when the walker is fooled, so omitting them
# traded away exactly the coverage it is for. A bare alias is followed by
# FROM / `,` / `;`, never by INTO / TABLE / ON / IMMEDIATE, so these cannot fire
# on `SELECT COUNT(*) merge FROM t`.
# A bare column/table alias is followed by a CLAUSE keyword, so the "verb needs
# an argument" patterns below must exclude those. Without this,
# `SELECT COUNT(*) call FROM t` matched `CALL <identifier>` (FROM is
# identifier-shaped) and legal SQL was refused.
_NOT_CLAUSE = (
    r"(?!(?:FROM|WHERE|GROUP|ORDER|JOIN|ON|LIMIT|OFFSET|HAVING|UNION|EXCEPT|"
    r"INTERSECT|QUALIFY|WINDOW|AS|USING|INNER|LEFT|RIGHT|FULL|CROSS|LATERAL|"
    r"AND|OR|IS|NOT|NULL|END|THEN|ELSE|WHEN)\b)"
)

_WRITE_COMMANDS_AFTER_PAREN = (
    r"MERGE\s+INTO\b",
    # `TABLE` is OPTIONAL in Snowflake's TRUNCATE, so match the bare form too.
    r"TRUNCATE\s+(?:TABLE\s+)?" + _NOT_CLAUSE + r"[A-Za-z_\"]",
    r"COMMENT\s+ON\s+(?:TABLE|VIEW|COLUMN|SCHEMA|DATABASE|WAREHOUSE|STAGE|"
    r"SEQUENCE|STREAM|TASK|PIPE|FUNCTION|PROCEDURE|ROLE|USER|INTEGRATION|"
    r"MATERIALIZED)\b",
    r"COPY\s+INTO\b",
    # UNDROP takes TABLE / SCHEMA / DATABASE; EXECUTE takes IMMEDIATE / TASK.
    r"UNDROP\s+(?:TABLE|SCHEMA|DATABASE)\b",
    r"EXECUTE\s+(?:IMMEDIATE|TASK)\b",
    # `RM` is REMOVE's documented alias; both take a stage reference.
    r"(?:REMOVE|RM)\s+@",
    r"PUT\s+file://",
    # CALL / UNLOAD / UNSET need an argument, which a bare alias never has:
    # an alias is followed by FROM / `,` / `;`, never by an identifier.
    r"CALL\s+" + _NOT_CLAUSE + r"[A-Za-z_\"$]",
    r"UNLOAD\s+(?:TO\s+)?@",
    r"UNSET\s+" + _NOT_CLAUSE + r"[A-Za-z_\"]",
)
_WRITE_VERB_AFTER_PAREN_RE = re.compile(
    r"(?<=\))\s*("
    + "|".join([*(v + r"\b" for v in _WRITE_VERBS_RESERVED), *_WRITE_COMMANDS_AFTER_PAREN])
    + r")",
    re.IGNORECASE,
)


def _valid_set_statement(stmt: str) -> bool:
    """Is *stmt* a `SET <var> = <expr>` with no command smuggled after it?

    ``_SET_STMT_RE`` only anchors the prefix, so `SET x = (SELECT 1) DELETE
    FROM t` matched it and the allowlist accepted the whole statement — every
    token after the `=` was examined by neither layer. Extending the tripwire's
    verb list chased that one verb at a time; checking the whole expression
    closes the class, including verbs nobody listed.

    An earlier attempt required the statement to END at the first balanced
    paren group, which refused the canonical idiom
    `SET end_date = (SELECT MAX(load_date) FROM V)::DATE` — and `- 1`, `/ 2`,
    `|| '…'`. Anchoring a window to a source's last loaded date, cast or
    adjusted, is the main reason `set_block` exists. So a leading group may be
    followed by more expression; what it may NOT contain is a write verb.

    Callers must pass MASKED text: a `)` or a verb-shaped word inside a literal
    must not affect the result.
    """
    m = _SET_STMT_RE.match(stmt)
    if not m:
        return False
    rest = stmt[m.end() :].strip()
    if not rest:
        return False  # `SET x = ;` — no expression at all
    # Scan the WHOLE expression, not just what follows a leading paren group:
    # wrapping the smuggled command in outer parens (`((SELECT 1) CALL p())`)
    # otherwise hid it inside the group and got past both layers. A write verb
    # has no place anywhere in a session-variable expression, and identifiers
    # that merely contain one (`UPDATES`, `create_date`) do not match on a word
    # boundary. Literals are already masked.
    return not re.search(r"\b(" + "|".join(_WRITE_VERBS) + r")\b", rest, re.IGNORECASE)


def _verify_read_only(text: str) -> list[str]:
    """Statement-root allowlist, plus a write-verb tripwire, over the file.

    ``WITH`` is only allowed when its terminal statement is a ``SELECT`` —
    a CTE can prefix DELETE/INSERT/UPDATE/MERGE, so the root alone proves
    nothing. Body lines that look like a provenance record are also refused:
    the check verb trusts exactly one final provenance line, so a template
    must never be able to plant a second.

    Defence in depth: the allowlist depends on parsing statement boundaries
    correctly, and that parsing has been defeated four times. So a write verb
    surviving masking as a bare token is refused independently of any parse
    (see ``_WRITE_VERBS``).
    """
    problems: list[str] = []
    masked_all = _mask_strings_and_comments(text)
    for stmt in _split_statements(masked_all):
        hits: list[tuple[int, str]] = []
        m0 = _WRITE_VERB_AT_START_RE.search(stmt)
        if m0:
            hits.append((m0.start(1), m0.group(1).upper()))
        # finditer, not search: a `search` that matched the leading `SET` and
        # then `continue`d on the SET exemption left EVERYTHING after the `=`
        # examined by neither layer, so `SET x = (SELECT 1) DELETE FROM t`
        # passed both. The SET exemption may only excuse the match at offset 0.
        hits += [
            (m.start(1), m.group(1).upper()) for m in _WRITE_VERB_AFTER_PAREN_RE.finditer(stmt)
        ]
        is_set_stmt = _valid_set_statement(stmt)
        for offset, verb in hits:
            if offset == 0 and verb == "SET" and is_set_stmt:
                continue  # the one legal write-shaped root; allowlist validates its form
            problems.append(
                f"write verb {verb!r} in command position — audit files are "
                "read-only. If this is an identifier rather than a command, "
                "quote it or introduce it with AS."
            )
    for line in text.splitlines():
        if line.lstrip().startswith("-- Provenance:"):
            problems.append("a review body line may not start with '-- Provenance:'")
    for stmt in _split_statements(text):
        root = stmt.split(None, 1)[0].upper() if stmt.split() else ""
        if root == "WITH":
            if _with_terminal_verb(stmt) == "SELECT":
                continue
            problems.append(
                "WITH statement does not terminate in SELECT — CTE-prefixed writes "
                "are not allowed in review SQL"
            )
            continue
        if root in ALLOWED_ROOTS - {"WITH"}:
            continue
        if root == "SET" and _valid_set_statement(stmt):
            continue
        problems.append(f"statement root {root or stmt[:20]!r} is not allowed in review SQL")
    return problems


def _inputs_digest(app: Path, manifest_path: Path, manifest: dict) -> str:
    """Digest of everything the rendered output is a function of.

    Recomputable WITHOUT importing anything: manifest bytes, every referenced
    query template's bytes, the app module files (manifest strategy), and the
    generator schema version.
    """
    h = hashlib.sha256()
    h.update(f"schema={GENERATOR_SCHEMA}".encode())
    h.update(manifest_path.read_bytes())
    for rel in sorted(_referenced_sources(manifest)):
        try:
            spath = _metric_source_path(app, rel) if not rel.startswith("queries/") else app / rel
        except ToolError:
            spath = None  # unresolvable/symlinked source digests as missing → drift
        h.update(f"\nsource:{rel}\n".encode())
        h.update(spath.read_bytes() if spath is not None and spath.is_file() else b"<missing>")
    if manifest.get("mode", "tokens") == "tokens" and manifest.get("token_strategy") == "manifest":
        # Conservative closure: hash EVERY app Python source, not just the
        # named modules. A dispatcher module can import sibling helpers, and
        # a dotted module name maps to a package path — enumerating the true
        # import closure without importing is not worth the failure mode of
        # missing one file and reporting a stale render as clean forever.
        for py in sorted(app.rglob("*.py")):
            # Relative to the app dir, NOT the absolute path: filtering on
            # absolute components meant a checkout under any dotted directory
            # (a worktree at `.claude/worktrees/<name>/`) skipped every module,
            # so the digest silently omitted the app closure. Provenance then
            # depended on WHERE the repo was checked out - the same commit
            # hashed differently in a worktree and a clean clone, so a
            # contributor saw false DRIFT - and locally this disabled the very
            # guarantee the loop exists for.
            if any(
                part.startswith(".") or part == "__pycache__" for part in py.relative_to(app).parts
            ):
                continue
            h.update(f"\nmodule:{py.relative_to(app).as_posix()}\n".encode())
            # A symlink pointing outside the app would hash whatever that
            # target happens to contain in THIS checkout, making provenance
            # environment-dependent again. Hash the link target's text instead:
            # deterministic everywhere, and a retarget still reads as a change.
            try:
                resolved = py.resolve(strict=True)
                external = not resolved.is_relative_to(app.resolve())
            except (OSError, RuntimeError):
                external = True
            if py.is_symlink() and external:
                # Hash the LINK, never the target's bytes: those are whatever
                # this machine happens to have outside the repo. A relative
                # link (what git stores) is identical in every checkout. An
                # absolute link is machine-specific by nature, so only its
                # basename is hashed — still deterministic for a given repo,
                # and a retarget to a different file is still a change.
                link = os.readlink(py)
                stable = link if not os.path.isabs(link) else "<abs>/" + os.path.basename(link)
                h.update(b"<external-symlink>" + stable.encode())
            else:
                h.update(py.read_bytes())
    return h.hexdigest()[:16]


def _referenced_queries(manifest: dict) -> set[str]:
    """Query NAMES (stems under queries/) this manifest claims for coverage."""
    out: set[str] = set()
    if manifest.get("mode", "tokens") == "metrics":
        # A metric whose authored block IS an app query (1:1 visual) claims it.
        for metric in manifest.get("metrics", []):
            source = str(metric.get("source", ""))
            if source.startswith("queries/"):
                out.add(Path(source).stem)
        return out
    specs = manifest.get("query_specs", {})
    for page in manifest.get("pages", []):
        for q in page.get("queries", []):
            spec = specs.get(q, {})
            out.add(spec.get("source_query", q))
    return out


def _referenced_sources(manifest: dict) -> set[str]:
    """App-relative source FILES the rendered output is a function of."""
    if manifest.get("mode", "tokens") == "metrics":
        return {str(m.get("source", "")) for m in manifest.get("metrics", []) if m.get("source")}
    return {f"queries/{name}.sql" for name in _referenced_queries(manifest)}


def _normalize_for_output_hash(text: str) -> str:
    """Normalize ONLY the volatile pieces, preserving every other byte.

    Exactly two things may differ between two legitimate generations of the
    same inputs: the Generated date, and the provenance record itself (which
    contains the output hash and so cannot be part of it). Everything else —
    including line endings and trailing whitespace — participates in the
    digest: a CRLF conversion or an appended statement after the provenance
    line is an edit, and must read as one. The split preserves ``\\r`` (we
    split on ``\\n`` only), so CRLF text hashes differently from the LF text
    the generator writes.
    """
    lines = []
    for line in text.split("\n"):
        stripped = line.rstrip("\r")
        if _GENERATED_RE.match(stripped):
            lines.append("-- Generated: <date> by streamsnow sql-review")
        elif stripped == _FINAL_PROVENANCE_PLACEHOLDER or _PROVENANCE_RE.match(stripped + "\n"):
            lines.append(_FINAL_PROVENANCE_PLACEHOLDER)
        else:
            lines.append(line)
    return "\n".join(lines)


_FINAL_PROVENANCE_PLACEHOLDER = "-- Provenance: <normalized>"


def _output_digest(text: str) -> str:
    return hashlib.sha256(_normalize_for_output_hash(text).encode()).hexdigest()[:16]


def parse_provenance(text: str) -> tuple[dict | None, str | None]:
    """Locate the single, FINAL provenance line. Returns (record, problem).

    Content after the provenance line — or a second provenance-shaped line
    anywhere — is how an edit could hide from the digest, so both are
    structural failures, never silently tolerated.
    """
    lines = text.split("\n")
    prov_idx = [i for i, ln in enumerate(lines) if ln.rstrip("\r").startswith("-- Provenance: ")]
    if not prov_idx:
        return None, "no provenance line — regenerate"
    if len(prov_idx) > 1:
        return None, "multiple provenance lines — the file was edited; regenerate"
    idx = prov_idx[0]
    if any(ln.strip() for ln in lines[idx + 1 :]):
        return None, "content after the provenance line — the file was edited; regenerate"
    m = _PROVENANCE_RE.match(lines[idx].rstrip("\r") + "\n")
    if not m:
        return None, "malformed provenance line — regenerate"
    return {"schema": m.group(1), "inputs": m.group(2), "output": m.group(3)}, None


def _out_filename(manifest: dict, combo_name: str, single_combo: bool) -> str:
    feature = manifest["feature"]
    return f"{feature}.review.sql" if single_combo else f"{feature}.{combo_name}.review.sql"


def render_review_file(
    app: Path,
    manifest_path: Path,
    manifest: dict,
    combo: dict,
    modules: dict,
) -> str:
    """Assemble the full review SQL text for one combo (no provenance yet)."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
    combo_keys = [k for k in combo if k not in {"name", "description", "notes"}]
    combo_summary = " ".join(f"{k}={combo[k]}" for k in combo_keys) or "(defaults)"
    # Sections are rendered FIRST so the SET block can be pruned to the variables
    # they actually reference and the header can describe what was really emitted.
    parts: list[str] = []
    dispatchers = manifest.get("token_dispatchers", {})
    specs = manifest.get("query_specs", {})
    binds_base = {**_DEFAULT_BINDS, **manifest.get("param_bindings", {})}

    rendered_queries: set[str] = set()
    for page in manifest.get("pages", []):
        for query_name in page.get("queries", []):
            if query_name in rendered_queries:
                continue  # a query on multiple pages renders once, first page wins
            rendered_queries.add(query_name)
            spec = specs.get(query_name, {})
            source = spec.get("source_query", query_name)
            qpath = app / "queries" / f"{source}.sql"
            if not qpath.is_file():
                raise ToolError(
                    f"query template queries/{source}.sql not found "
                    f"(manifest {manifest_path.name}, query {query_name!r})"
                )
            body = strip_header(qpath.read_text(encoding="utf-8"))

            tokens_needed = spec.get("tokens")
            if tokens_needed is None:
                tokens_needed = template_tokens(qpath.read_text(encoding="utf-8"))
            token_lines: list[str] = []
            for tok in tokens_needed:
                dispatcher = (spec.get("token_overrides") or {}).get(tok) or dispatchers.get(tok)
                if dispatcher is None:
                    raise ToolError(
                        f"no dispatcher for token {tok!r} (query {query_name!r}) — "
                        "add it to token_dispatchers"
                    )
                body = body.replace(
                    "{" + tok + "}", _resolve_token(tok, dispatcher, combo, modules)
                )
                token_lines.append(_dispatcher_banner(tok, dispatcher, combo))

            leftover = template_tokens(body)
            if leftover:
                raise ToolError(
                    f"query {query_name!r} still contains unresolved tokens {leftover} after "
                    "rendering — a review file that errors on paste is worse than none"
                )

            binds = {**binds_base, **spec.get("bind_overrides", {})}
            # Exactly one trailing `;` so editors split statements cleanly and
            # a doubled `;;` never halts a batched run as an empty statement.
            runnable = _substitute_binds(body, binds).rstrip().rstrip(";").rstrip() + ";"

            sublines = [
                f"Page:   {page['name']}",
                f"Params: {spec.get('params_doc', '(none)')}",
            ]
            if spec.get("notes"):
                sublines.append(f"Notes:  {spec['notes']}")
            if token_lines:
                sublines.append("Tokens applied:")
                sublines.extend(f"  {t}" for t in token_lines)
            parts.append(_banner(f"[Page: {page['name']}] {source}.sql", sublines))
            parts.append("")
            # The one-line tag names the on-screen visual, so running the block
            # labels its result to match the dashboard.
            parts.append(f"-- {spec.get('metric_name', f'{source}.sql')}")
            parts.append(runnable)
            parts.append("")

    set_block = _set_block(manifest, "\n".join(parts))
    header = _banner(
        f"{manifest['feature'].upper()} SQL REVIEW — apps/{app.name} (generated)",
        [
            f"Generated: {timestamp} by streamsnow sql-review",
            f"Feature:   {manifest['feature']}",
            f"Combo:     {combo['name']}  {combo_summary}",
            f"Notes:     {combo.get('description', '')}",
            "",
            "Each section is a fully-rendered, paste-and-runnable query.",
            *_bind_note(bool(set_block)),
        ],
    )
    prefix = [header, ""] + ([set_block, ""] if set_block else [])

    text = (
        "\n".join(line.rstrip() for line in "\n".join(prefix + parts).splitlines()).rstrip() + "\n"
    )
    problems = (
        _verify_read_only(text) + _verify_binds_bound(text) + _verify_session_vars_defined(text)
    )
    if problems:
        raise ToolError(
            f"refusing to write {manifest['feature']!r} review SQL: " + "; ".join(problems)
        )
    return text


def _stamp_provenance(text: str, inputs: str) -> str:
    """Append the final provenance line.

    The output digest is computed over the file WITH the provenance line
    normalized to its placeholder — exactly the transformation ``check``
    applies to the finished file — so both sides hash the same string.
    """
    body = text.rstrip() + "\n"
    output = _output_digest(body + _FINAL_PROVENANCE_PLACEHOLDER + "\n")
    return body + f"-- Provenance: schema={GENERATOR_SCHEMA} inputs={inputs} output={output}\n"


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
def _duplicate_fragments(app: Path) -> list[str]:
    """Fragment stems declared by MORE THAN ONE manifest.

    Per-manifest validation rejects a duplicate within one file; across files
    nothing noticed, and the first manifest's ``reason`` silently won — so the
    index reported one rationale while another manifest asserted a different
    one. Ownership of an exemption should be unambiguous.
    """
    seen: dict[str, int] = {}
    for mp in _manifest_paths(app):
        with contextlib.suppress(ToolError):
            for entry in load_manifest(mp).get("fragments") or []:
                if isinstance(entry, dict) and isinstance(entry.get("file"), str):
                    fname = entry["file"]
                    if "/" not in fname and "\\" not in fname and fname.endswith(".sql"):
                        stem = fname[: -len(".sql")]
                        seen[stem] = seen.get(stem, 0) + 1
    return sorted(k for k, n in seen.items() if n > 1)


def _declared_fragments(app: Path) -> dict[str, str]:
    """Query files a manifest declares as CTE fragments, mapped to the reason.

    A shared-CTE file (``queries/_region_ctes.sql``) is inlined into other
    queries via a token and is NOT independently runnable, so it can never be
    "claimed" by a manifest the way a real query is — yet coverage counted it
    as an uncovered gap, which makes the gate unsatisfiable for any repo that
    factors CTEs into their own files.

    Exemption is explicit, never inferred from the filename: a bare
    leading-underscore convention would let anyone silence the gate by
    renaming a query. Declaring one also preserves WHY it is a fragment, which
    is exactly the knowledge that a per-file naming convention loses.
    """
    frags: dict[str, str] = {}
    for mp in _manifest_paths(app):
        with contextlib.suppress(ToolError):
            for entry in load_manifest(mp).get("fragments") or []:
                if not (isinstance(entry, dict) and isinstance(entry.get("file"), str)):
                    continue
                fname = entry["file"]
                # Bare filenames only — never Path().stem, which would collapse
                # `../../x.sql` onto `queries/x.sql` and exempt the wrong file.
                if "/" in fname or "\\" in fname or not fname.endswith(".sql"):
                    continue
                frags.setdefault(fname[: -len(".sql")], str(entry.get("reason", "")).strip())
    return frags


def coverage(app: Path) -> dict:
    """Which queries/*.sql are claimed by a manifest, and which are not."""
    claimed: set[str] = set()
    for mp in _manifest_paths(app):
        with contextlib.suppress(ToolError):
            claimed |= _referenced_queries(load_manifest(mp))
    all_queries = {p.stem for p in _query_files(app)}
    fragments = _declared_fragments(app)
    exempt = all_queries & set(fragments)
    # A query claimed by a page in ONE manifest and declared a fragment in
    # ANOTHER is a contradiction. Per-manifest validation cannot see it, since
    # it only checks the manifest in hand. Left alone, the query was both
    # claimed and exempt and cmd_index emitted two conflicting README rows for
    # it, the fragment row reading `Verified: n/a` — so a reviewer could read a
    # page-feeding query as out of scope. Resolve toward COVERAGE (the stricter
    # reading) and report it, rather than silently honouring the exemption.
    conflicting = exempt & claimed
    exempt -= conflicting
    return {
        "queries": sorted(all_queries),
        "claimed": sorted(all_queries & claimed),
        "uncovered": sorted(all_queries - claimed - exempt),
        "fragments": sorted(exempt),
        "fragments_conflicting": sorted(conflicting),
        "fragment_reasons": {k: fragments[k] for k in sorted(exempt)},
        # A fragment declared but absent from queries/ is a stale declaration —
        # surfaced so a deleted fragment cannot quietly keep its exemption.
        "fragments_missing": sorted(set(fragments) - all_queries),
    }


# --------------------------------------------------------------------------- #
# Verbs
# --------------------------------------------------------------------------- #
def cmd_discover(args: argparse.Namespace) -> int:
    app = _app_dir(Path(args.dir).resolve(), args.slug)
    cov = coverage(app)
    proposals = []
    for name in cov["uncovered"]:
        qpath = app / "queries" / f"{name}.sql"
        text = qpath.read_text(encoding="utf-8")
        header = parse_header(text)
        tokens = template_tokens(text)
        skeleton = {
            "schema_version": 1,
            "feature": re.sub(r"[^a-z0-9_-]", "-", name.lower()),
            "app": app.name,
            "description": header.get("Feeds", f"companion for {name}.sql"),
            "token_strategy": "static",
            "token_dispatchers": {
                t: {"literal": f"-- TODO: sample fragment for {t}"} for t in tokens
            },
            "combos": [{"name": "all-default", "description": "default filters"}],
            "pages": [{"name": header.get("Feeds", "Unknown page"), "queries": [name]}],
            "query_specs": {name: {"params_doc": header.get("Params", "(none)")}},
        }
        proposals.append(skeleton)
        if args.write:
            mdir = _review_dir(app) / "manifests"
            mdir.mkdir(parents=True, exist_ok=True)
            out = mdir / f"{skeleton['feature']}.json"
            if not out.exists():
                out.write_text(json.dumps(skeleton, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"app": app.name, "coverage": cov, "proposed_manifests": proposals}, indent=2))
    return 1 if cov["uncovered"] else 0


def _manifest_outputs(manifest: dict) -> list[str]:
    """The review filenames one manifest produces (metrics: exactly one)."""
    if manifest.get("mode", "tokens") == "metrics":
        return [f"{manifest['feature']}.review.sql"]
    combos = manifest.get("combos") or [{"name": "all-default"}]
    single = len(combos) == 1
    return [_out_filename(manifest, c["name"], single) for c in combos]


def _metric_source_path(app: Path, rel: str) -> Path:
    """Resolve a metric source with containment + no-symlink checks.

    The manifest regex constrains the LEXICAL path, but reads follow symlinks:
    a valid-looking ``sql_review/_metrics/x.sql`` symlink could pull any
    readable file into the rendered review SQL (and the digest). Reject
    symlinks outright and require the resolved path to stay inside the app.
    """
    spath = app / rel
    if spath.is_symlink():
        raise ToolError(f"metric source {rel!r} is a symlink — not allowed")
    try:
        resolved = spath.resolve()
        if not resolved.is_relative_to(app.resolve()):
            raise ToolError(f"metric source {rel!r} resolves outside the app")
    except OSError as exc:
        raise ToolError(f"metric source {rel!r}: {exc}") from exc
    return spath


def render_metrics_file(app: Path, manifest: dict) -> str:
    """Assemble the per-visual review file (manifest ``mode: "metrics"``).

    One runnable block per dashboard visual, in on-screen order, from AUTHORED
    SQL files (``sql_review/_metrics/*.sql``, or a ``queries/*.sql`` where a
    visual is 1:1 with an app query) — the mode for dashboards whose visuals
    aggregate differently than any single app query. Each block's ``-- <name>``
    tag is the on-screen visual name, so running it labels the result to match
    the dashboard; a dashboard-map index sits up top. Never imports anything —
    metrics blocks are static by nature.
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d")
    metrics = manifest["metrics"]
    # Blocks are rendered FIRST so the SET block can be pruned to the variables
    # they actually reference and the header can describe what was really emitted.
    parts: list[str] = []
    binds_base = {**_DEFAULT_BINDS, **manifest.get("param_bindings", {})}
    for m in metrics:
        spath = _metric_source_path(app, m["source"])
        if not spath.is_file():
            raise ToolError(f"metric source not found: {m['source']} (metric {m['name']!r})")
        body = strip_header(spath.read_text(encoding="utf-8"))
        binds = {**binds_base, **m.get("bind_overrides", {})}
        runnable = _substitute_binds(body, binds).rstrip().rstrip(";").rstrip() + ";"
        sublines = [f"Page:   {m['page']}", f"Visual: {m['title']}", f"Source: {m['source']}"]
        if m.get("params_doc"):
            sublines.append(f"Params: {m['params_doc']}")
        if m.get("notes"):
            sublines.append(f"Notes:  {m['notes']}")
        parts.append(_banner(f"[{m['page']}] {m['title']}", sublines))
        parts.append("")
        parts.append(f"-- {m['name']}")
        parts.append(runnable)
        parts.append("")

    set_block = _set_block(manifest, "\n".join(parts))
    header = _banner(
        f"{manifest['feature'].upper()} SQL REVIEW — apps/{app.name} (generated, per-visual)",
        [
            f"Generated: {timestamp} by streamsnow sql-review",
            f"Feature:   {manifest['feature']}",
            "Mode:      metrics — one runnable block per dashboard visual, in on-screen order.",
            "",
            "Each block's comment line (-- <name>) is the on-screen visual name, so",
            "running it labels the result tab to match the dashboard.",
            *_bind_note(bool(set_block)),
        ],
    )
    map_rows = [f"{(m['page'] + ' > ' + m['title']):<52} {m['name']}" for m in metrics]
    prefix = [header, ""]
    if set_block:
        prefix += [set_block, ""]
    prefix += [_banner("DASHBOARD MAP (in on-screen order)", map_rows), ""]

    text = (
        "\n".join(line.rstrip() for line in "\n".join(prefix + parts).splitlines()).rstrip() + "\n"
    )
    problems = (
        _verify_read_only(text) + _verify_binds_bound(text) + _verify_session_vars_defined(text)
    )
    if problems:
        raise ToolError(
            f"refusing to write {manifest['feature']!r} metrics review SQL: " + "; ".join(problems)
        )
    return text


def cmd_generate(args: argparse.Namespace) -> int:
    repo = Path(args.dir).resolve()
    app = _app_dir(repo, args.slug)
    manifests = _manifest_paths(app)
    if args.feature:
        manifests = [m for m in manifests if m.stem == args.feature]
        if not manifests:
            raise ToolError(f"no manifest sql_review/manifests/{args.feature}.json")
    if not manifests:
        raise ToolError(
            f"no manifests under {_review_dir(app) / 'manifests'} — run "
            f"`streamsnow sql-review discover {args.slug} --write` first"
        )

    # Collision gate across ALL of the app's manifests (not just the subset
    # being regenerated): two manifests producing the same filename would
    # silently clobber each other's audit trail.
    for fname, owner_list in _output_owners(app).items():
        if len(owner_list) > 1:
            raise ToolError(
                f"output collision: {fname} is produced by {', '.join(owner_list)} — "
                "give each manifest a distinct feature (or distinct combo names)"
            )

    written: list[str] = []
    for mp in manifests:
        manifest = load_manifest(mp)
        if manifest.get("app") not in (None, app.name):
            raise ToolError(f"{mp}: manifest app={manifest.get('app')!r} != {app.name!r}")
        inputs = _inputs_digest(app, mp, manifest)
        produced: set[str] = set()
        if manifest.get("mode", "tokens") == "metrics":
            text = render_metrics_file(app, manifest)
            out = _review_dir(app) / _manifest_outputs(manifest)[0]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_stamp_provenance(text, inputs), encoding="utf-8")
            produced.add(out.name)
            written.append(str(out.relative_to(repo)))
        else:
            modules: dict = {}
            if manifest.get("token_strategy") == "manifest":
                modules = _import_modules(app, manifest)
            combos = manifest.get("combos") or [{"name": "all-default", "description": "defaults"}]
            single = len(combos) == 1
            for combo in combos:
                text = render_review_file(app, mp, manifest, combo, modules)
                out = _review_dir(app) / _out_filename(manifest, combo["name"], single)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(_stamp_provenance(text, inputs), encoding="utf-8")
                produced.add(out.name)
                written.append(str(out.relative_to(repo)))
        # A combo removed from the manifest takes its rendered file with it —
        # a stale generated file nobody accounts for is unexamined surface.
        feature = manifest["feature"]
        for stale in _review_dir(app).glob(f"{feature}.*review.sql"):
            if stale.name not in produced:
                stale.unlink()
                print(f"removed stale {stale.relative_to(repo)}")
    for w in written:
        print(f"wrote {w}")
    return 0


def _output_owners(app: Path) -> dict[str, list[str]]:
    """Map review filename -> the manifest files that would produce it.

    Two manifests sharing a ``feature`` (or overlapping combo names) would
    silently overwrite each other's rendered files and still read clean —
    every caller must treat len(owners) > 1 as a failure.
    """
    owners: dict[str, list[str]] = {}
    for mp in _manifest_paths(app):
        with contextlib.suppress(ToolError):
            manifest = load_manifest(mp)
            for fname in _manifest_outputs(manifest):
                owners.setdefault(fname, []).append(mp.name)
    return owners


def _check_app(repo: Path, app: Path) -> list[dict]:
    findings: list[dict] = []
    cov = coverage(app)
    findings += [
        {
            "file": f"apps/{app.name}/queries/{q}.sql",
            "line": 1,
            "detail": "query not claimed by any sql_review manifest — every UI-feeding "
            "query needs a human-runnable companion (streamsnow sql-review discover)",
        }
        for q in cov["uncovered"]
    ]

    findings += [
        {
            "file": f"apps/{app.name}/sql_review/manifests",
            "line": 1,
            "detail": f"manifest declares fragment {f!r} but queries/{f}.sql does not "
            "exist — drop the stale declaration so a deleted file cannot keep its "
            "coverage exemption",
        }
        for f in cov.get("fragments_missing", [])
    ]
    findings += [
        {
            "file": f"apps/{app.name}/sql_review/manifests",
            "line": 1,
            "detail": f"query {f!r} is claimed by a page in one manifest and declared a "
            "fragment in another — a query is either runnable or an inlined fragment, "
            "not both; the exemption is ignored until this is resolved",
        }
        for f in cov.get("fragments_conflicting", [])
    ]
    findings += [
        {
            "file": f"apps/{app.name}/sql_review/manifests",
            "line": 1,
            "detail": f"fragment {f!r} is declared by more than one manifest — the first "
            "reason silently wins and the index then states only one of them; declare it "
            "in exactly one manifest",
        }
        for f in _duplicate_fragments(app)
    ]

    # Cross-manifest collisions read as findings here too, so the gate can
    # never report clean while one manifest's trail overwrites another's.
    owners = _output_owners(app)
    for fname, owner_list in owners.items():
        if len(owner_list) > 1:
            findings.append(
                {
                    "file": f"apps/{app.name}/sql_review/{fname}",
                    "line": 1,
                    "detail": "output collision — produced by "
                    f"{', '.join(owner_list)}; give each manifest a distinct feature",
                }
            )

    # Orphans: a review file no manifest accounts for is unexamined surface —
    # a combo removed from a manifest must take its rendered file with it.
    expected = set(owners)
    review_dir = _review_dir(app)
    if review_dir.is_dir():
        for stray in sorted(review_dir.glob("*.review.sql")):
            if stray.name not in expected:
                findings.append(
                    {
                        "file": f"apps/{app.name}/sql_review/{stray.name}",
                        "line": 1,
                        "detail": "orphaned review file — no current manifest produces it; "
                        f"delete it or re-run `streamsnow sql-review generate {app.name}`",
                    }
                )

    # Static audit of the committed text itself. Provenance hashes prove a file
    # matches its inputs; they do not prove the file RUNS. A hand-edited trail,
    # or one generated before a guard existed, can carry an unsubstituted bind
    # or a write statement while hashing perfectly — so check the bytes too.
    # Import-free: pure text analysis, no consumer code executed.
    if review_dir.is_dir():
        for rf in sorted(review_dir.glob("*.review.sql")):
            try:
                raw = rf.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # unreadable files are reported by the provenance pass
            # Strip the trailing provenance line first: the write guard rejects
            # a `-- Provenance:` line anywhere but last, and a committed file
            # legitimately ends with one. Same predicate as parse_provenance.
            body = "\n".join(
                ln for ln in raw.splitlines() if not ln.rstrip("\r").startswith("-- Provenance: ")
            )
            # Only the bind audit here. The read-only allowlist already runs over
            # committed bodies in the provenance pass below; re-running it made
            # one planted DELETE report three times, which buries a real finding.
            for detail in _verify_binds_bound(body) + _verify_session_vars_defined(body):
                findings.append(
                    {
                        "file": f"apps/{app.name}/sql_review/{rf.name}",
                        "line": 1,
                        "detail": detail,
                    }
                )

    for mp in _manifest_paths(app):
        try:
            manifest = load_manifest(mp)
        except ToolError as exc:
            findings.append({"file": str(mp.relative_to(repo)), "line": 1, "detail": str(exc)})
            continue
        inputs = _inputs_digest(app, mp, manifest)
        for fname in _manifest_outputs(manifest):
            out = _review_dir(app) / fname
            rel = f"apps/{app.name}/sql_review/{out.name}"
            if not out.is_file():
                findings.append(
                    {
                        "file": rel,
                        "line": 1,
                        "detail": "review file missing — run "
                        f"`streamsnow sql-review generate {app.name}`",
                    }
                )
                continue
            try:
                text = out.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # A structured finding, never a traceback: the gate runs
                # inside pre-commit/CI/validate and must stay exit-1-shaped.
                findings.append(
                    {"file": rel, "line": 1, "detail": f"unreadable review file ({exc})"}
                )
                continue
            record, problem = parse_provenance(text)
            if record is None:
                findings.append({"file": rel, "line": 1, "detail": problem})
                continue
            if record["inputs"] != inputs:
                findings.append(
                    {
                        "file": rel,
                        "line": 1,
                        "detail": "DRIFT: manifest/template/module inputs changed since "
                        f"generation — run `streamsnow sql-review generate {app.name}`",
                    }
                )
            if record["output"] != _output_digest(text):
                findings.append(
                    {
                        "file": rel,
                        "line": 1,
                        "detail": "review file body was edited by hand — regenerate (the "
                        "manifest is the editing surface, not the rendered file)",
                    }
                )
            # Belt over the digest: the committed file must ALSO still be
            # read-only SQL, whatever its provenance says. The single final
            # provenance line (already structurally validated) is exempt.
            body_only = "\n".join(
                ln for ln in text.split("\n") if not ln.rstrip("\r").startswith("-- Provenance: ")
            )
            for problem_line in _verify_read_only(body_only):
                findings.append({"file": rel, "line": 1, "detail": problem_line})
    return findings


def cmd_check(args: argparse.Namespace) -> int:
    repo = Path(args.dir).resolve()
    if args.slug:
        apps = [_app_dir(repo, args.slug)]
    else:
        apps_root = repo / "apps"
        apps = (
            sorted(p for p in apps_root.iterdir() if (p / "snowflake.yml").is_file())
            if apps_root.is_dir()
            else []
        )
    findings: list[dict] = []
    for app in apps:
        findings += _check_app(repo, app)
    result = {"ok": not findings, "findings": findings}
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("sql-review: clean")
    else:
        for f in findings:
            print(f"FAIL {f['file']}:{f['line']} {f['detail']}")
    return 0 if result["ok"] else 1


_README_TABLE_START = "<!-- sql-review-index:start -->"
_README_TABLE_END = "<!-- sql-review-index:end -->"


def cmd_index(args: argparse.Namespace) -> int:
    """Rebuild the README coverage table between the index markers.

    The tool owns the table skeleton (it cannot drift from the manifests);
    the lineage narrative around it — authored by the review recipe — is
    preserved byte-for-byte. A "verified" column value other than the default
    is carried over per row, keyed by query name.
    """
    repo = Path(args.dir).resolve()
    app = _app_dir(repo, args.slug)
    readme = _review_dir(app) / "README.md"
    existing = readme.read_text(encoding="utf-8") if readme.is_file() else ""

    # The markers must be unambiguous before anything is rewritten: with
    # duplicated or reordered markers, a first-occurrence splice silently
    # swallows narrative (observed in review). Line-anchored, count-checked.
    start_count = sum(1 for ln in existing.splitlines() if ln.strip() == _README_TABLE_START)
    end_count = sum(1 for ln in existing.splitlines() if ln.strip() == _README_TABLE_END)
    if (start_count, end_count) not in ((0, 0), (1, 1)):
        raise ToolError(
            f"README has {start_count} start / {end_count} end index markers — "
            "expected exactly one pair (or none); fix the README before re-indexing"
        )
    has_markers = start_count == 1
    start_idx = end_idx = -1
    if has_markers:
        lines = existing.splitlines()
        start_idx = next(i for i, ln in enumerate(lines) if ln.strip() == _README_TABLE_START)
        end_idx = next(i for i, ln in enumerate(lines) if ln.strip() == _README_TABLE_END)
        if end_idx < start_idx:
            raise ToolError("README index markers are reversed — fix the README")
        block_lines = lines[start_idx + 1 : end_idx]
    else:
        block_lines = []

    # Carry over the human-maintained columns (upstream, verified) — reading
    # ONLY the marked block. An unrelated 5-column table elsewhere in the
    # narrative must never overwrite a reviewer's sign-off (observed in review).
    carried: dict[str, tuple[str, str]] = {}
    for line in block_lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0] not in ("Query", "---", ""):
            carried[cells[0].strip("`")] = (cells[1], cells[4])

    rows: list[str] = [
        "| Query | Upstream object(s) | Feeds | Review file | Verified |",
        "|---|---|---|---|---|",
    ]
    for mp in _manifest_paths(app):
        try:
            manifest = load_manifest(mp)
        except ToolError:
            continue
        review_file = _manifest_outputs(manifest)[0]
        if manifest.get("mode", "tokens") == "metrics":
            for m in manifest.get("metrics", []):
                upstream, verified = carried.get(
                    m["name"], ("_(fill via /review-app --sql)_", "no")
                )
                rows.append(
                    f"| `{m['name']}` | {upstream} | {m['page']} > {m['title']} | "
                    f"`{review_file}` | {verified} |"
                )
            continue
        for page in manifest.get("pages", []):
            for q in page.get("queries", []):
                upstream, verified = carried.get(q, ("_(fill via /review-app --sql)_", "no"))
                rows.append(
                    f"| `{q}` | {upstream} | {page['name']} | `{review_file}` | {verified} |"
                )
    cov = coverage(app)
    for q in cov["uncovered"]:
        upstream, _ = carried.get(q, ("_(fill via /review-app --sql)_", "no"))
        rows.append(f"| `{q}` | {upstream} | — | **UNCOVERED** | no |")
    # Declared CTE fragments are listed WITH their reason rather than hidden:
    # a reader who greps this index for a query file must find out why it has
    # no runnable companion, not merely that it is absent.
    for q in cov.get("fragments", []):
        reason = cov.get("fragment_reasons", {}).get(q) or "shared CTE fragment"
        rows.append(f"| `{q}` | — | — | _fragment — {reason}_ | n/a |")

    table = "\n".join([_README_TABLE_START, *rows, _README_TABLE_END])
    if has_markers:
        lines = existing.splitlines()
        new = "\n".join([*lines[:start_idx], table, *lines[end_idx + 1 :]])
        if existing.endswith("\n"):
            new += "\n"
    elif existing:
        new = existing.rstrip() + "\n\n## Coverage\n\n" + table + "\n"
    else:
        new = (
            f"# SQL review — apps/{app.name}\n\n"
            "Paste-and-runnable companions for every UI-feeding query. Open a review\n"
            "file, run the SET block, then any section, and compare against the visual\n"
            "it feeds (see docs/auditing-a-visual.md in the StreamSnow docs).\n\n"
            "## Coverage\n\n" + table + "\n"
        )
    readme.parent.mkdir(parents=True, exist_ok=True)
    readme.write_text(new, encoding="utf-8")
    print(f"wrote {readme.relative_to(repo)}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sql_review",
        description="Generate and verify human-runnable sql_review/ companions.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="Propose skeleton manifests for uncovered queries.")
    p.add_argument("slug")
    p.add_argument("--dir", default=".", help="Repo root (default: cwd).")
    p.add_argument("--write", action="store_true", help="Persist proposed skeleton manifests.")

    p = sub.add_parser("generate", help="Render review files from manifests (+ provenance).")
    p.add_argument("slug")
    p.add_argument("--dir", default=".")
    p.add_argument("--feature", default=None, help="Only this manifest.")

    p = sub.add_parser("check", help="Import-free freshness + coverage gate.")
    p.add_argument("slug", nargs="?", default=None, help="App slug (default: every app).")
    p.add_argument("--dir", default=".")
    p.add_argument("--format", choices=("md", "json"), default="md")

    p = sub.add_parser("index", help="Rebuild the README coverage table.")
    p.add_argument("slug")
    p.add_argument("--dir", default=".")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dispatch = {
        "discover": cmd_discover,
        "generate": cmd_generate,
        "check": cmd_check,
        "index": cmd_index,
    }
    try:
        return dispatch[args.cmd](args)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
