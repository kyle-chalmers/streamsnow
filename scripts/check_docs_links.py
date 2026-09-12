#!/usr/bin/env python3
"""Opt-in, online sweep of every official-docs URL in docs/snowflake-docs.md.

Not part of the test suite (tests are offline by contract). Run before a
release, per RELEASING.md::

    uv run python scripts/check_docs_links.py --online

Exit 0 when every URL answers 200 without redirecting; 1 otherwise (a 308 means
Snowflake moved the page — update the registry to the new canonical path).
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "docs" / "snowflake-docs.md"
_URL_RE = re.compile(r"https://docs\.(?:snowflake\.com|streamlit\.io)/[^\s<>()\[\]`'\"|]*")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--online", action="store_true", help="Actually fetch (required).")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args(argv)
    if not args.online:
        print("pass --online to fetch; this script makes network calls by design.")
        return 2
    urls = sorted({m.group(0).rstrip(".,;:") for m in _URL_RE.finditer(REGISTRY.read_text())})
    opener = urllib.request.build_opener(_NoRedirect)
    bad = 0
    for url in urls:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "streamsnow-docs-check"}
        )
        try:
            with opener.open(req, timeout=args.timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (urllib.error.URLError, TimeoutError) as exc:
            status = f"ERR {exc}"
        ok = status == 200
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {status} {url}")
    print(f"{len(urls) - bad}/{len(urls)} ok")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
