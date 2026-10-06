"""The migrate conform contract: the conform scanners and the gates agree on "done".

/migrate-app gets an external app in, /build-app's phases conform it, and the gates
(`validate-app`, `sql-review check`) decide when it's done (skills/migrate-app/SKILL.md).
That only holds if `streamsnow migrate scan-conformance` / `scan-inline-sql` and the gates
judge the same app the same way. If a scanner and a gate drift (a scanner keeps flagging an
app every gate passes, or empties its worklist while a gate still fails), the skill's
"Done when" can't be met or is met by a broken app. This walks one fictional Acme app
through the lift and the conform and checks both sides at each end.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from typer.testing import CliRunner

from streamsnow.cli import app
from streamsnow.config import Config
from streamsnow.policy import SchemaPolicy
from streamsnow.tools import sql_review
from streamsnow.tools.migrate_app import (
    graft_plan,
    preflight,
    scan_conformance,
    scan_hardfails,
    scan_imports,
    scan_inline_sql,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "streamsnow.config.example.yaml"
SLUG = "acme-revenue"
TABLE = "ANALYTICS_DB.ANALYTICS.ORDERS"
runner = CliRunner()


def _cfg() -> Config:
    return Config.from_dict(yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8")))


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _external_source(root: Path) -> Path:
    """An external Acme dashboard: legacy pages/ layout, an uncached fetch with inline
    SQL, SELECT * and an altair chart. Every item on the conform worklist."""
    src = root / "acme-external"
    _write(src / "app.py", 'import streamlit as st\n\nst.title("Acme revenue")\n')
    _write(
        src / "pages" / "10_revenue.py",
        "import altair as alt\nimport streamlit as st\n\n\n"
        "def load_orders():\n"
        '    conn = st.connection("snowflake")\n'
        f'    return conn.query("SELECT * FROM {TABLE}")\n\n\n'
        "orders = load_orders()\n"
        'st.altair_chart(alt.Chart(orders).mark_line().encode(x="ORDER_DATE", y="NET_PAID"))\n',
    )
    _write(src / "requirements.txt", "streamlit==1.50.0\naltair\n")
    return src


def _init_repo(root: Path) -> None:
    args = ["init", "--no-starter-app", "--config", str(EXAMPLE_CONFIG), "--dir", str(root)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output


def _lift(src: Path, target: Path) -> None:
    """Step 1: copy per the graft plan; the entrypoint becomes streamlit_app.py."""
    _write(target / "streamlit_app.py", (src / "app.py").read_text(encoding="utf-8"))
    shutil.copytree(src / "pages", target / "pages")


def _copy_scaffold_modules(scratch_app: Path, target: Path) -> None:
    """Step 2, foundation: everything a fresh scaffold has that the lift lacks. Never
    overwrite a source file."""
    for path in scratch_app.rglob("*"):
        dest = target / path.relative_to(scratch_app)
        if path.is_file() and not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)


def _conform(a: Path, scaffold_entry: str) -> None:
    """Step 2, conform: what a page-builder in `mode: conform` writes, and the
    orchestrator's nav and index. The visual (net paid by day) is unchanged."""
    # The scaffold's starter page and query came along with the modules; the migrated
    # page replaces them (build-app pages.md § Replace the starter trio).
    (a / "pages/overview.py").unlink()
    (a / "queries/example_metric.sql").unlink()
    (a / "pages/10_revenue.py").unlink()
    _write(
        a / "queries/orders_by_day.sql",
        "-- Query: orders_by_day\n-- Feeds: Revenue (net paid by day)\n"
        "-- Schemas: ANALYTICS_DB.ANALYTICS\n-- Params: :1 start_date, :2 end_date\n"
        "SELECT\n    order_date,\n    SUM(net_paid) AS net_paid\n"
        f"FROM {TABLE}\n"
        "WHERE order_date BETWEEN :1 AND :2\nGROUP BY order_date\n",
    )
    _write(
        a / "pages/revenue.py",
        '"""Revenue."""\n\nimport streamlit as st\nfrom review import review_value\n'
        "from sql_loader import load_sql\n\n\n"
        "@st.cache_data(ttl=1800)\n"
        "def load_orders(start: str, end: str):\n"
        '    sql = load_sql("orders_by_day")\n'
        '    return st.connection("snowflake").query(sql, params=[start, end], ttl=0)\n\n\n'
        'st.title("Acme revenue")\n'
        'orders = load_orders("2024-01-01", "2024-12-31")\n'
        'st.line_chart(review_value("net_paid_by_day", orders), x="ORDER_DATE", y="NET_PAID")\n',
    )
    # The scaffold's entrypoint (st.navigation) replaces the legacy one.
    _write(
        a / "streamlit_app.py",
        scaffold_entry.replace(
            'st.Page("pages/overview.py", title="Overview"',
            'st.Page("pages/revenue.py", title="Revenue"',
        ),
    )
    _write(
        a / "sql_review/index.yaml",
        "schema_version: 2\n"
        f"app: {SLUG}\n"
        "review_window:\n"
        f"  start_date: \"(SELECT DATEADD('year', -1, MAX(order_date)) FROM {TABLE})::DATE\"\n"
        f'  end_date: "(SELECT MAX(order_date) FROM {TABLE})::DATE"\n'
        "pages:\n"
        "  - path: pages/revenue.py\n"
        "    metrics:\n"
        "      - key: net_paid_by_day\n"
        "        query: queries/orders_by_day.sql\n"
        '        binds: {"1": params.start_date, "2": params.end_date}\n'
        f"        reads: [{TABLE}]\n"
        "  - path: pages/about.py\n"
        "    metrics: []\n",
    )


def _validate() -> dict:
    result = runner.invoke(app, ["validate-app", SLUG, "--format", "json"])
    assert result.exit_code in (0, 1), result.output
    return {"exit": result.exit_code, **json.loads(result.output)}


def test_lift_then_conform_scanners_and_gates_agree(tmp_path, monkeypatch):
    src = _external_source(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    a = repo / "apps" / SLUG

    # Step 1: the lift verbs accept the source.
    assert preflight(src, SLUG, repo)[0] == 0
    assert graft_plan(src)[1]["graft_target"] == "pages/*"
    assert scan_imports(src)[1]["relative_imports"] == []
    policy = SchemaPolicy.from_governance(_cfg().governance)
    assert scan_hardfails(src, policy)[1]["blocks"] is False
    _lift(src, a)

    # Step 2 starts from a scratch scaffold's modules (skills/migrate-app/SKILL.md step 8).
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _init_repo(scratch)
    monkeypatch.chdir(scratch)
    assert runner.invoke(app, ["new", "acme", "revenue"]).exit_code == 0
    scratch_app = scratch / "apps" / SLUG
    _copy_scaffold_modules(scratch_app, a)
    monkeypatch.chdir(repo)

    # Before the conform, the scanners hold a worklist and the gate fails.
    _, before = scan_conformance(a, _cfg())
    assert [u["func"] for u in before["uncached_queries"]] == ["load_orders"]
    assert before["select_stars"] and before["altair_imports"]
    assert before["legacy_pages_only"] is True
    assert scan_inline_sql(a)[1]["candidates"]
    pre = _validate()
    assert pre["exit"] == 1
    assert "caching" in [c["name"] for c in pre["checks"] if not c["ok"]]  # the uncached fetch

    # After it, both sides are clean.
    _conform(a, (scratch_app / "streamlit_app.py").read_text(encoding="utf-8"))
    assert sql_review.main(["generate", SLUG, "--dir", str(repo)]) == 0
    _, after = scan_conformance(a, _cfg())
    assert after["uncached_queries"] == []
    assert after["select_stars"] == []
    assert after["altair_imports"] == []
    assert after["legacy_pages_only"] is False
    assert scan_inline_sql(a)[1]["candidates"] == []
    assert scan_hardfails(a, policy)[1]["blocks"] is False
    report = _validate()
    assert report["exit"] == 0, report
    # starter-text is the warn-only reminder to rewrite AGENTS.md and the README Apps row;
    # every other check must carry no warning.
    assert not [
        w for c in report["checks"] if c["name"] != "starter-text" for w in c.get("warnings", [])
    ]
    assert sql_review.main(["check", SLUG, "--dir", str(repo)]) == 0
