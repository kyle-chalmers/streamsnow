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
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

#: Bumped when the rendered-file format changes shape — makes every prior
#: file read as drift, which is correct: format changes need a regenerate.
GENERATOR_SCHEMA = 1

#: Statement roots a review file may contain. SET is restricted separately
#: to session-variable assignments (see _verify_read_only).
ALLOWED_ROOTS = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"})

_SET_STMT_RE = re.compile(r"^SET\s+[A-Za-z_][A-Za-z0-9_$]*\s*=", re.IGNORECASE)
_PROVENANCE_RE = re.compile(
    r"^-- Provenance: schema=(\d+) inputs=([0-9a-f]{16}) output=([0-9a-f]{16})\s*$",
    re.MULTILINE,
)
_GENERATED_RE = re.compile(r"^-- Generated: \d{4}-\d{2}-\d{2} by streamsnow sql-review$")

_HEADER_FIELD_RE = re.compile(r"^--\s*(Query|Feeds|Schemas|Params|Tokens):\s*(.*)$")
_TOKEN_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_BIND_RE = re.compile(r"(?<!:):(\d+)\b")  # skip :: casts; word-boundary right

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


def validate_manifest(m: dict) -> list[str]:
    """Schema-validate one manifest dict. Returns problem strings (empty = ok)."""
    out: list[str] = []
    if m.get("schema_version") != 1:
        out.append(f"schema_version must be 1 (got {m.get('schema_version')!r})")
    feature = m.get("feature", "")
    if not isinstance(feature, str) or not _FEATURE_RE.match(feature):
        out.append(f"feature {feature!r} must match ^[a-z][a-z0-9_-]*$")
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


def _set_block(manifest: dict) -> str:
    pairs = manifest.get("set_block") or _DEFAULT_SET
    lines = [
        "-- Edit these SET lines to change the review window. Every section below",
        "-- references the session variables — no per-section edits required.",
    ]
    lines += [f"SET {name} = {expr};" for name, expr in pairs.items()]
    for sv in manifest.get("set_vars", []):
        if sv.get("comment"):
            lines.append(f"-- {sv['comment']}")
        lines.append(f"SET {sv['name']} = {sv['default']};")
    return "\n".join(lines)


def _mask_strings_and_comments(text: str) -> str:
    """Replace string-literal contents and comments with spaces, same length.

    Every structural decision downstream (statement splitting, paren
    balancing, verb extraction) runs on the MASKED text — a ``)`` or ``;`` or
    verb-shaped word inside a string literal must never influence structure.
    This closed a real bypass: ``WITH x AS (SELECT ')SELECT' …) DELETE …``
    fooled a raw paren counter into reading the literal's contents as the
    terminal verb. Handles ``''`` escaping; an unterminated literal masks to
    end-of-text, which downstream reads as "cannot parse" → not allowed.
    Length and newlines are preserved so nothing shifts.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":  # string literal
            i += 1
            while i < n:
                if text[i] == "'" and i + 1 < n and text[i + 1] == "'":
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                if text[i] == "'":
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
        m = re.match(r"(?:RECURSIVE\s+)?[A-Za-z_][A-Za-z0-9_$]*", stmt[i:], re.IGNORECASE)
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


def _verify_read_only(text: str) -> list[str]:
    """Statement-root allowlist over the whole rendered file.

    ``WITH`` is only allowed when its terminal statement is a ``SELECT`` —
    a CTE can prefix DELETE/INSERT/UPDATE/MERGE, so the root alone proves
    nothing. Body lines that look like a provenance record are also refused:
    the check verb trusts exactly one final provenance line, so a template
    must never be able to plant a second.
    """
    problems: list[str] = []
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
        if root == "SET" and _SET_STMT_RE.match(stmt):
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
    for name in sorted(_referenced_queries(manifest)):
        qpath = app / "queries" / f"{name}.sql"
        h.update(f"\nquery:{name}\n".encode())
        h.update(qpath.read_bytes() if qpath.is_file() else b"<missing>")
    if manifest.get("token_strategy") == "manifest":
        # Conservative closure: hash EVERY app Python source, not just the
        # named modules. A dispatcher module can import sibling helpers, and
        # a dotted module name maps to a package path — enumerating the true
        # import closure without importing is not worth the failure mode of
        # missing one file and reporting a stale render as clean forever.
        for py in sorted(app.rglob("*.py")):
            if any(part.startswith(".") or part == "__pycache__" for part in py.parts):
                continue
            h.update(f"\nmodule:{py.relative_to(app).as_posix()}\n".encode())
            h.update(py.read_bytes())
    return h.hexdigest()[:16]


def _referenced_queries(manifest: dict) -> set[str]:
    out: set[str] = set()
    specs = manifest.get("query_specs", {})
    for page in manifest.get("pages", []):
        for q in page.get("queries", []):
            spec = specs.get(q, {})
            out.add(spec.get("source_query", q))
    return out


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
    header = _banner(
        f"{manifest['feature'].upper()} SQL REVIEW — apps/{app.name} (generated)",
        [
            f"Generated: {timestamp} by streamsnow sql-review",
            f"Feature:   {manifest['feature']}",
            f"Combo:     {combo['name']}  {combo_summary}",
            f"Notes:     {combo.get('description', '')}",
            "",
            "Each section is a fully-rendered, paste-and-runnable query.",
            "Bind params are replaced with session variables (see the SET block);",
            "edit the SET lines once to change the review window for every section.",
        ],
    )

    parts: list[str] = [header, "", _set_block(manifest), ""]
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

    text = "\n".join(line.rstrip() for line in "\n".join(parts).splitlines()).rstrip() + "\n"
    problems = _verify_read_only(text)
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
def coverage(app: Path) -> dict:
    """Which queries/*.sql are claimed by a manifest, and which are not."""
    claimed: set[str] = set()
    for mp in _manifest_paths(app):
        with contextlib.suppress(ToolError):
            claimed |= _referenced_queries(load_manifest(mp))
    all_queries = {p.stem for p in _query_files(app)}
    return {
        "queries": sorted(all_queries),
        "claimed": sorted(all_queries & claimed),
        "uncovered": sorted(all_queries - claimed),
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
        modules: dict = {}
        if manifest.get("token_strategy") == "manifest":
            modules = _import_modules(app, manifest)
        inputs = _inputs_digest(app, mp, manifest)
        combos = manifest.get("combos") or [{"name": "all-default", "description": "defaults"}]
        single = len(combos) == 1
        produced: set[str] = set()
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
            combos = manifest.get("combos") or [{"name": "all-default"}]
            single = len(combos) == 1
            for c in combos:
                owners.setdefault(_out_filename(manifest, c["name"], single), []).append(mp.name)
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

    for mp in _manifest_paths(app):
        try:
            manifest = load_manifest(mp)
        except ToolError as exc:
            findings.append({"file": str(mp.relative_to(repo)), "line": 1, "detail": str(exc)})
            continue
        inputs = _inputs_digest(app, mp, manifest)
        combos = manifest.get("combos") or [{"name": "all-default"}]
        single = len(combos) == 1
        for combo in combos:
            out = _review_dir(app) / _out_filename(manifest, combo["name"], single)
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
        combos = manifest.get("combos") or [{"name": "all-default"}]
        single = len(combos) == 1
        review_file = _out_filename(manifest, combos[0]["name"], single)
        for page in manifest.get("pages", []):
            for q in page.get("queries", []):
                upstream, verified = carried.get(q, ("_(fill via /review-app --sql)_", "no"))
                rows.append(
                    f"| `{q}` | {upstream} | {page['name']} | `{review_file}` | {verified} |"
                )
    for q in coverage(app)["uncovered"]:
        upstream, _ = carried.get(q, ("_(fill via /review-app --sql)_", "no"))
        rows.append(f"| `{q}` | {upstream} | — | **UNCOVERED** | no |")

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
