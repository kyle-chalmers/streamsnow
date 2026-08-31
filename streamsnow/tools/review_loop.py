#!/usr/bin/env python3
"""Review-loop primitives for `/review-app --auto`.

Deterministic logic the auto-review loop needs every cycle, extracted so
the skill doesn't re-derive it from prose each invocation — prose loops
drift, and a loop that re-invents its dedup each cycle re-reports findings
it already resolved. Subcommands:

    parse-findings <report.md>
        Parse a /review-app review-<ts>.md report into a JSON list of
        findings (dimension, severity, citation, summary, why).

    dedup-findings <session-dir> --new=<report.md>
        Build the (citation, normalized_summary) set from every prior
        ``## Resolutions`` block in review artifacts under <session-dir>
        within a 7-day freshness window. Filter the new report's findings
        to only those NOT in the set. Output: JSON list of deduped
        findings. Dedup is against everything previously RESOLVED — not
        merely previously confirmed — or judge-rejected findings reappear
        every round and the loop never converges.

    write-resolutions <report.md> --applied=<json> --deferred-b=<json>
                                  --bucket-c=<json> [--out-of-scope=<json>]
        Append a ``## Resolutions`` block to the report using the schema
        `/review-app --fix` writes. <json> args are either file paths or
        ``-`` (stdin).

    exit-condition --iter=N --max-iter=N --applied=N
                   [--block=N --flag=N]
                   [--walk-status=CLEAN|DEGRADED --walk-not-clean
                    --walk-findings-new=N --walk-reentries=N
                    --max-walk-reentries=N]
        Decide whether the loop should continue and why. Output JSON
        ``{exit_loop, reason, walk_clean, walk_status}`` with reason in
        {max-iterations, walk-degraded, clean, walk-reentry, plateau,
        continue}. Exit code 0 = continue, 1 = done. The walk flags let a
        browser walkthrough re-open the loop when it finds mechanically
        fixable defects; omit them all and behavior matches a walk-free loop.

    merge-findings --inputs=<agent1>:<path1>[,<agent2>:<path2>...]
        Merge findings from multiple reviewer reports (e.g. Claude plus
        other local coding agents), dedup on ``(citation,
        normalized_summary)``, tag each finding with its first-flagging
        agent, mark ``(also flagged by X)`` when ≥2 agents flagged the same
        tuple. Output: a single combined merged report on stdout.

All output is JSON unless explicitly noted. Exit code 2 = tool error.

Design notes:
- The report schema this tool parses is the one `/review-app` emits and
  `/review-app --fix` consumes (see ``skills/review-app/`` in the plugin).
- ``normalize_summary`` collapses whitespace and lowercases so that two
  reports phrasing the same finding slightly differently still dedup.
- Artifact filenames are matched case-insensitively (``review-*.md`` and
  the uppercase dialect both count) so artifacts from other tooling that
  follow the same schema participate in dedup.
"""

from __future__ import annotations

import argparse
import enum
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


#: Statuses a browser walk may report. Anything else is treated as
#: DEGRADED (untrusted) by exit-condition — fail toward "UI unverified".
WALK_STATUSES = frozenset({"CLEAN", "DEGRADED"})


class Severity(enum.StrEnum):
    BLOCK = "BLOCK"
    FLAG = "FLAG"
    NICE_TO_HAVE = "NICE-TO-HAVE"


@dataclass(frozen=True)
class Finding:
    dimension: str  # e.g. "SQL" | "Data Flow" | "UI" | "Runtime" | "Docs"
    severity: str  # BLOCK | FLAG | NICE-TO-HAVE
    citation: str  # "file:line" or "DB.SCHEMA.OBJECT"
    summary: str  # short why-it-matters phrase
    why: str = ""  # extended detail if present
    agent: str = ""  # reviewer name (claude, or another local agent CLI)
    also_flagged_by: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_DIMENSION_RE = re.compile(r"^##\s+(?P<title>[^\n]+?)\s*$", re.MULTILINE)
_BUCKET_RE = re.compile(r"^###\s+(?P<severity>BLOCK|FLAG|NICE-TO-HAVE)\s*$", re.MULTILINE)
_FINDING_LINE_RE = re.compile(
    r"""
    ^[\-*]\s+                       # list bullet
    (?:\[(?P<citation>[^\]]+)\]\s+)? # optional [file:line]
    (?P<body>.+?)\s*$               # rest of the line
    """,
    re.MULTILINE | re.VERBOSE,
)


def normalize_summary(summary: str) -> str:
    """Lowercase, collapse whitespace — for dedup tuple matching."""
    return re.sub(r"\s+", " ", summary).strip().lower()


def parse_findings(text: str) -> list[Finding]:
    """Parse a review report body into a list of Finding objects.

    The schema is fixed by /review-app: top-level ``## <Dimension>``
    sections, each containing one or more ``### BLOCK | FLAG |
    NICE-TO-HAVE`` subsections, each containing zero or more
    ``- [file:line] <summary> — <why>`` lines. A line of ``- _none_``
    means the bucket is empty.

    Resolutions sections (``## Resolutions``) are deliberately skipped
    here — they're consumed by dedup-findings separately.
    """
    findings: list[Finding] = []
    sections = _split_sections(text)
    for dim_title, body in sections.items():
        if dim_title.lower() == "resolutions":
            continue
        for severity, items in _split_buckets(body).items():
            for item in items:
                finding = _parse_item(dim_title, severity, item)
                if finding is not None:
                    findings.append(finding)
    return findings


def _split_sections(text: str) -> dict[str, str]:
    """Return {dimension_title: body_text}. Order preserved by dict insertion."""
    out: dict[str, str] = {}
    matches = list(_DIMENSION_RE.finditer(text))
    for i, m in enumerate(matches):
        title = m.group("title").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[title] = text[start:end]
    return out


def _split_buckets(body: str) -> dict[str, list[str]]:
    """Return {severity: [line, line, ...]} for one dimension body."""
    out: dict[str, list[str]] = {s.value: [] for s in Severity}
    matches = list(_BUCKET_RE.finditer(body))
    for i, m in enumerate(matches):
        sev = m.group("severity")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        bucket_text = body[start:end]
        for line in bucket_text.splitlines():
            line = line.strip()
            if not line or line.startswith("- _none_") or line.startswith("* _none_"):
                continue
            if line.startswith("- ") or line.startswith("* "):
                out[sev].append(line)
    return out


def _parse_item(dimension: str, severity: str, line: str) -> Finding | None:
    m = _FINDING_LINE_RE.match(line)
    if not m:
        return None
    citation = (m.group("citation") or "").strip()
    body = m.group("body").strip()
    # Split body on " — " (em-dash) into summary / why.
    if " — " in body:
        summary, why = body.split(" — ", 1)
    elif " -- " in body:
        summary, why = body.split(" -- ", 1)
    else:
        summary, why = body, ""
    return Finding(
        dimension=dimension,
        severity=severity,
        citation=citation,
        summary=summary.strip(),
        why=why.strip(),
    )


# ---------------------------------------------------------------------------
# Resolutions parsing (for dedup)
# ---------------------------------------------------------------------------


_RESOLUTIONS_HEADER_RE = re.compile(r"^##\s+Resolutions\s*$", re.MULTILINE)


def parse_resolution_tuples(text: str) -> set[tuple[str, str]]:
    """Extract (citation, normalized_summary) tuples from a report's
    ``## Resolutions`` block. Looks for citation + summary in the same
    schema `/review-app --fix` writes:

        ### Applied / Deferred / Bucket-C / Out-of-scope
        - [file:line] <summary> — <why>
    """
    out: set[tuple[str, str]] = set()
    matches = list(_RESOLUTIONS_HEADER_RE.finditer(text))
    if not matches:
        return out
    for m in matches:
        start = m.end()
        # Resolutions block runs to next ## section or end of file.
        next_match = _DIMENSION_RE.search(text, pos=start)
        end = next_match.start() if next_match else len(text)
        block = text[start:end]
        for line in block.splitlines():
            line = line.strip()
            if not line or not (line.startswith("- ") or line.startswith("* ")):
                continue
            fl = _FINDING_LINE_RE.match(line)
            if not fl:
                continue
            citation = (fl.group("citation") or "").strip()
            body = fl.group("body").strip()
            if " — " in body:
                summary, _ = body.split(" — ", 1)
            elif " -- " in body:
                summary, _ = body.split(" -- ", 1)
            else:
                summary = body
            if citation:
                out.add((citation, normalize_summary(summary)))
    return out


def _is_review_report(path: Path) -> bool:
    """Case-insensitive review-report filename match (both artifact dialects)."""
    name = path.name.lower()
    return name.endswith(".md") and name.startswith(("review-", "loop-"))


def collect_resolution_tuples(
    session_dir: Path,
    window_days: int = 7,
) -> set[tuple[str, str]]:
    """Walk review reports in session_dir within the freshness window
    and union all their Resolutions tuples.
    """
    cutoff = time.time() - (window_days * 86400)
    out: set[tuple[str, str]] = set()
    if not session_dir.is_dir():
        return out
    for path in session_dir.iterdir():
        if not path.is_file() or not _is_review_report(path):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        out |= parse_resolution_tuples(text)
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_parse_findings(args: argparse.Namespace) -> int:
    path = Path(args.report)
    if not path.is_file():
        print(json.dumps({"error": f"not a file: {path}"}), file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8")
    findings = parse_findings(text)
    print(json.dumps([asdict(f) for f in findings], indent=2))
    return 0


def cmd_dedup_findings(args: argparse.Namespace) -> int:
    session_dir = Path(args.session_dir)
    new_report = Path(args.new)
    if not new_report.is_file():
        print(json.dumps({"error": f"not a file: {new_report}"}), file=sys.stderr)
        return 2

    resolved = collect_resolution_tuples(session_dir, window_days=args.window_days)
    new_findings = parse_findings(new_report.read_text(encoding="utf-8"))
    kept = [f for f in new_findings if (f.citation, normalize_summary(f.summary)) not in resolved]
    print(json.dumps([asdict(f) for f in kept], indent=2))
    return 0


def _load_json_list(spec: str | None) -> list[dict[str, Any]]:
    """Read a JSON list from a file path, ``-`` stdin, or treat empty as []."""
    if not spec:
        return []
    if spec == "-":
        text = sys.stdin.read()
    else:
        p = Path(spec)
        if not p.is_file():
            return []
        text = p.read_text(encoding="utf-8")
    if not text.strip():
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{spec!r} did not contain a JSON list")
    return data


def _format_finding_line(f: dict[str, Any]) -> str:
    """Render one finding dict as the canonical ``- [cite] summary — why`` line."""
    cite = f.get("citation") or ""
    summary = f.get("summary") or ""
    why = f.get("why") or ""
    head = f"[{cite}] " if cite else ""
    if why:
        return f"- {head}{summary} — {why}"
    return f"- {head}{summary}"


def cmd_write_resolutions(args: argparse.Namespace) -> int:
    """Append a ``## Resolutions`` block to a report."""
    report_path = Path(args.report)
    if not report_path.is_file():
        print(json.dumps({"error": f"not a file: {report_path}"}), file=sys.stderr)
        return 2

    applied = _load_json_list(args.applied)
    deferred_b = _load_json_list(args.deferred_b)
    bucket_c = _load_json_list(args.bucket_c)
    out_of_scope = _load_json_list(args.out_of_scope)

    lines: list[str] = ["", "## Resolutions", ""]
    sections = [
        ("Applied (Bucket A)", applied),
        ("Deferred — judgment required (Bucket B)", deferred_b),
        ("Bucket C — out of scope / wontfix", bucket_c),
        ("Out-of-scope (pages filter)", out_of_scope),
    ]
    for title, items in sections:
        lines.append(f"### {title}")
        if not items:
            lines.append("- _none_")
        else:
            for f in items:
                lines.append(_format_finding_line(f))
        lines.append("")

    with report_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        if not lines[-1]:
            fh.write("\n")

    counts = {
        "applied": len(applied),
        "deferred_b": len(deferred_b),
        "bucket_c": len(bucket_c),
        "out_of_scope": len(out_of_scope),
    }
    print(json.dumps(counts, indent=2))
    return 0


def cmd_exit_condition(args: argparse.Namespace) -> int:
    """Decide whether the loop should continue.

    Priority order:

      1. max-iterations — iter >= max_iter (hard ceiling, checked first)
      2. walk-degraded  — the browser walk could not be trusted
      3. clean          — block + flag == 0 AND walk_clean
      4. walk-reentry   — the walk found mechanically-fixable defects
      5. plateau        — applied this cycle == 0 and no walk re-entry available
      6. continue

    ``max-iterations`` is deliberately checked FIRST. When it was third, a cycle
    that hit the ceiling while still finding auto-fixable work reported
    ``clean``/``plateau`` instead, hiding the fact that the loop ran out of
    budget rather than out of findings.

    A DEGRADED walk is TERMINAL, never a re-entry: a walk that can't be
    trusted (missing browser, un-seeded auth) produces zero findings, and
    looping on it would spin forever verifying nothing.

    Walk re-entry has two independent brakes: ``--walk-reentries`` against
    ``--max-walk-reentries``, and ``--walk-findings-new`` (findings whose
    ``(citation, normalized_summary)`` key the loop has not already attempted).
    A flapping page cannot ping-pong because a finding it already drove a
    re-entry for is no longer "new".
    """
    block = args.block or 0
    flag = args.flag or 0
    # walk_clean defaults True so callers that don't run a walk (--no-final-walk,
    # browser tooling absent) are unaffected by this logic.
    walk_clean = not args.walk_not_clean
    # Allow-list, not a DEGRADED check: an unrecognized status (typo, a status a
    # future walk emits, stray whitespace) must count as untrusted. Treating
    # anything-not-DEGRADED as trustworthy would let `--walk-status=FAILED`
    # report `clean` and claim the UI was verified when it wasn't.
    walk_status = args.walk_status.strip().upper()
    if walk_status not in WALK_STATUSES:
        walk_status = "DEGRADED"
    walk_degraded = walk_status == "DEGRADED"
    reentry_available = args.walk_findings_new > 0 and args.walk_reentries < args.max_walk_reentries

    if args.iter >= args.max_iter:
        verdict = {"exit_loop": True, "reason": "max-iterations"}
    elif walk_degraded:
        verdict = {"exit_loop": True, "reason": "walk-degraded"}
    elif block + flag == 0 and walk_clean:
        verdict = {"exit_loop": True, "reason": "clean"}
    elif reentry_available:
        verdict = {"exit_loop": False, "reason": "walk-reentry"}
    elif args.applied == 0:
        verdict = {"exit_loop": True, "reason": "plateau"}
    else:
        verdict = {"exit_loop": False, "reason": "continue"}

    verdict["walk_clean"] = walk_clean
    verdict["walk_status"] = walk_status
    print(json.dumps(verdict, indent=2))
    # Exit code: 0 = continue (skill loops), 1 = done (skill breaks).
    return 0 if not verdict["exit_loop"] else 1


def _parse_inputs_spec(spec: str) -> dict[str, Path]:
    """Parse ``agent1:path1,agent2:path2`` → {agent1: Path1, ...}."""
    out: dict[str, Path] = {}
    if not spec:
        return out
    for pair in spec.split(","):
        if ":" not in pair:
            raise ValueError(f"bad input spec {pair!r}; expected agent:path")
        agent, path = pair.split(":", 1)
        out[agent.strip()] = Path(path.strip())
    return out


def cmd_merge_findings(args: argparse.Namespace) -> int:
    """Merge per-agent reports into a consensus-tagged combined report."""
    inputs = _parse_inputs_spec(args.inputs)
    if not inputs:
        print(json.dumps({"error": "no --inputs spec"}), file=sys.stderr)
        return 2

    # agent -> list[Finding]
    per_agent: dict[str, list[Finding]] = {}
    for agent, path in inputs.items():
        if not path.is_file():
            print(
                json.dumps({"error": f"missing {agent} report: {path}"}),
                file=sys.stderr,
            )
            return 2
        findings = parse_findings(path.read_text(encoding="utf-8"))
        per_agent[agent] = [
            Finding(
                dimension=f.dimension,
                severity=f.severity,
                citation=f.citation,
                summary=f.summary,
                why=f.why,
                agent=agent,
            )
            for f in findings
        ]

    # Bucket by (dimension, severity, citation, normalized_summary).
    # First agent to flag wins; later agents become also_flagged_by.
    by_key: dict[tuple[str, str, str, str], Finding] = {}
    for agent in inputs:
        for f in per_agent[agent]:
            key = (f.dimension, f.severity, f.citation, normalize_summary(f.summary))
            if key not in by_key:
                by_key[key] = f
            else:
                existing = by_key[key]
                also = list(existing.also_flagged_by)
                if agent not in also and agent != existing.agent:
                    also.append(agent)
                by_key[key] = Finding(
                    dimension=existing.dimension,
                    severity=existing.severity,
                    citation=existing.citation,
                    summary=existing.summary,
                    why=existing.why,
                    agent=existing.agent,
                    also_flagged_by=tuple(also),
                )

    merged = list(by_key.values())
    _render_merged_report(merged)
    return 0


def _render_merged_report(findings: list[Finding]) -> None:
    """Render a merged report to stdout in the /review-app schema with
    per-finding agent attribution tags."""
    # Group by dimension preserving order of first appearance.
    by_dim: dict[str, list[Finding]] = {}
    for f in findings:
        by_dim.setdefault(f.dimension, []).append(f)

    print("# Merged review report\n")
    print(
        f"Findings from {len({f.agent for f in findings})} reviewer(s); "
        f"deduped + consensus-tagged.\n"
    )
    for dim, items in by_dim.items():
        print(f"## {dim}\n")
        for severity in (Severity.BLOCK.value, Severity.FLAG.value, Severity.NICE_TO_HAVE.value):
            print(f"### {severity}")
            bucket = [f for f in items if f.severity == severity]
            if not bucket:
                print("- _none_")
            else:
                for f in bucket:
                    tags = [f"({f.agent.title()})"]
                    if f.also_flagged_by:
                        tags.append(
                            "(also flagged by "
                            + ", ".join(a.title() for a in f.also_flagged_by)
                            + ")"
                        )
                    head = f"[{f.citation}] " if f.citation else ""
                    why = f" — {f.why}" if f.why else ""
                    print(f"- {head}{f.summary}{why} {' '.join(tags)}")
            print()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_loop",
        description="Deterministic primitives for the /review-app --auto loop.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse-findings", help="Parse a review-<ts>.md into JSON findings.")
    p.add_argument("report", help="Path to a review report file")

    p = sub.add_parser("dedup-findings", help="Filter a new report by prior Resolutions.")
    p.add_argument("session_dir", help="Directory of review reports (apps/<slug>/.review/)")
    p.add_argument("--new", required=True, help="The new report to filter")
    p.add_argument("--window-days", type=int, default=7)

    p = sub.add_parser("write-resolutions", help="Append a ## Resolutions block to a report.")
    p.add_argument("report")
    p.add_argument("--applied", default="", help="Path to JSON list, or '-' for stdin")
    p.add_argument("--deferred-b", default="")
    p.add_argument("--bucket-c", default="")
    p.add_argument("--out-of-scope", default="")

    p = sub.add_parser("exit-condition", help="Should the loop continue?")
    p.add_argument("--iter", type=int, required=True)
    p.add_argument("--max-iter", type=int, required=True)
    p.add_argument("--applied", type=int, required=True)
    p.add_argument("--block", type=int, default=0)
    p.add_argument("--flag", type=int, default=0)
    p.add_argument(
        "--walk-status",
        default="CLEAN",
        help="CLEAN or DEGRADED, from the walk report header (default CLEAN)",
    )
    p.add_argument(
        "--walk-not-clean",
        action="store_true",
        help="Set when the walk produced BLOCK/FLAG findings (walk_clean=false)",
    )
    p.add_argument(
        "--walk-findings-new",
        type=int,
        default=0,
        help="Mechanically-fixable walk findings not already attempted in a prior re-entry",
    )
    p.add_argument("--walk-reentries", type=int, default=0, help="Re-entries used so far")
    p.add_argument("--max-walk-reentries", type=int, default=2)

    p = sub.add_parser("merge-findings", help="Merge per-agent reports with consensus tags.")
    p.add_argument(
        "--inputs",
        required=True,
        help="Comma-separated agent:path pairs (e.g. claude:a.md,other:b.md)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "parse-findings": cmd_parse_findings,
        "dedup-findings": cmd_dedup_findings,
        "write-resolutions": cmd_write_resolutions,
        "exit-condition": cmd_exit_condition,
        "merge-findings": cmd_merge_findings,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
