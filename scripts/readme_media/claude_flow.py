"""Scenes for demo.gif: the StreamSnow journey as it happens in Claude, then CI, then Snowflake.

The README GIF should show what a user actually does, which is talk to Claude, not type CLI
commands. The words in these scenes are condensed from one real end-to-end run (a TPC-H sales
dashboard built with /build-app, reviewed, previewed, shipped with /ship-app, merged and
deployed by CI), with every account, user and repository identifier replaced by the fictional
acme-analytics names the README already uses. The app pictures are that run's own preview
screenshots, kept in assets/sales-performance/ so the GIF regenerates without a Snowflake
account. Scene text is HTML built here; render_gif.py screenshots and stitches it.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets" / "sales-performance"
SHOTS = {name: ASSETS / f"{name}.png" for name in ("overview", "regions", "products")}
SHOT_W = 1280  # the screenshots' pixel width
SHOT_TOOLBAR = 52  # Streamlit's local "Deploy" toolbar, in screenshot pixels
REPO_NAME = "acme-analytics"
APP_SLUG = "sales-performance"
APP_OBJECT = "STREAMSNOW_APPS.DASHBOARDS.SALES_PERFORMANCE"
PR_TITLE = "feat(sales-performance): add sales performance dashboard"

CSS = """
  .app { position:absolute; top:56px; left:24px; right:24px; bottom:20px; border-radius:10px;
    overflow:hidden; background:#262624; color:#ECEAE4;
    box-shadow:0 18px 50px rgba(0,0,0,.45), 0 0 0 1px rgba(232,244,250,.08); }
  .app .bar { background:#1F1E1D; color:#A8A49B; }
  .side { position:absolute; top:30px; left:0; bottom:0; width:176px; background:#1F1E1D;
    border-right:1px solid #34332F; padding:12px 10px; box-sizing:border-box; font-size:12.5px; }
  .side .new { border:1px solid #3D3C37; border-radius:8px; padding:7px 10px; color:#ECEAE4; }
  .side h6 { margin:16px 4px 6px; font-size:10.5px; font-weight:600; color:#7D7A72;
    text-transform:uppercase; letter-spacing:.06em; }
  .side .s { padding:7px 10px; border-radius:7px; color:#A8A49B; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis; }
  .side .s.on { background:#34332F; color:#ECEAE4; }
  .main { position:absolute; top:30px; left:176px; right:0; bottom:0; }
  .feed { position:absolute; top:0; left:0; right:0; bottom:66px; padding:10px 34px 8px;
    display:flex; flex-direction:column; justify-content:flex-end; gap:10px; overflow:hidden; }
  .feed > * { flex-shrink:0; }
  .u { align-self:flex-end; max-width:72%; background:#3A3935; border-radius:14px;
    padding:8px 13px; font-size:14px; line-height:1.4; }
  .u code, .c code { font-family:"JetBrains Mono","DejaVu Sans Mono",Menlo,monospace;
    font-size:12.5px; color:#F0B49C; }
  .c { display:flex; gap:10px; font-size:14px; line-height:1.45; max-width:94%; }
  .c .mark { color:#D97757; font-size:17px; line-height:19px; flex-shrink:0; }
  .c b { color:#FFFFFF; font-weight:600; }
  .tools { display:flex; flex-direction:column; gap:4px; margin-left:27px; }
  .tool { display:flex; align-items:center; gap:8px; font-size:12.5px; color:#B9B5AC;
    font-family:"JetBrains Mono","DejaVu Sans Mono",Menlo,monospace; }
  .tool .st { width:14px; text-align:center; font-weight:700; }
  .tool .st.ok { color:#5BBF77; } .tool .st.run { color:#D97757; }
  .tool .k { color:#7D7A72; }
  .ask { margin-left:27px; border:1px solid #4A4843; border-radius:10px; padding:10px 12px;
    background:#2C2B28; max-width:80%; }
  .ask .h { display:inline-block; font-size:10.5px; font-weight:700; letter-spacing:.05em;
    text-transform:uppercase; color:#1F1E1D; background:#D97757; border-radius:4px;
    padding:1px 6px; margin-bottom:6px; }
  .ask .q { font-size:13.5px; margin-bottom:7px; }
  .ask .o { font-size:12.5px; color:#B9B5AC; border:1px solid #3D3C37; border-radius:7px;
    padding:5px 9px; margin-top:4px; }
  .ask .o.pick { border-color:#D97757; color:#ECEAE4; background:rgba(217,119,87,.12); }
  .wire { margin-left:27px; font-family:"JetBrains Mono","DejaVu Sans Mono",Menlo,monospace;
    font-size:12px; line-height:1.5; color:#CFCBC2; background:#1F1E1D; border-radius:8px;
    padding:8px 12px; white-space:pre; }
  .composer { position:absolute; left:34px; right:34px; bottom:14px; height:42px;
    border:1px solid #45443F; border-radius:12px; background:#30302D; display:flex;
    align-items:center; padding:0 8px 0 14px; font-size:14px; }
  .composer .ph { color:#7D7A72; }
  .composer .send { margin-left:auto; width:28px; height:28px; border-radius:8px;
    background:#D97757; color:#1F1E1D; display:flex; align-items:center;
    justify-content:center; font-weight:700; font-size:15px; }
  .composer .send.off { background:#4A4843; color:#7D7A72; }
  .caret { display:inline-block; width:2px; height:17px; background:#ECEAE4;
    vertical-align:-3px; margin-left:1px; }

  .gh { position:absolute; top:56px; left:24px; right:24px; bottom:20px; border-radius:10px;
    overflow:hidden; background:#0D1117; color:#E6EDF3;
    box-shadow:0 18px 50px rgba(0,0,0,.45), 0 0 0 1px rgba(232,244,250,.08); }
  .gh .bar { background:#161B22; color:#8B949E; }
  .gh .url { position:absolute; left:50%; transform:translateX(-50%); top:5px; height:20px;
    width:440px; border-radius:6px; background:#0D1117; color:#8B949E; font-size:12px;
    display:flex; align-items:center; justify-content:center; }
  .pr { position:absolute; top:30px; left:0; right:0; bottom:0; padding:20px 30px; }
  .pr h2 { margin:0; font-size:22px; font-weight:600; }
  .pr h2 span { color:#8B949E; font-weight:400; }
  .pill { display:inline-flex; align-items:center; gap:6px; border-radius:999px;
    padding:4px 11px; font-size:13px; font-weight:600; color:#fff; margin:10px 10px 0 0; }
  .pill.open { background:#238636; } .pill.merged { background:#8957E5; }
  .meta { color:#8B949E; font-size:13px; }
  .box { border:1px solid #30363D; border-radius:8px; margin-top:16px; overflow:hidden; }
  .box .hd { padding:10px 14px; font-size:14px; font-weight:600; border-bottom:1px solid #30363D;
    display:flex; align-items:center; gap:10px; }
  .box .row { padding:8px 14px; font-size:13px; display:flex; align-items:center; gap:10px;
    border-bottom:1px solid #21262D; color:#C9D1D9; }
  .box .row:last-child { border-bottom:0; }
  .box .row .d { margin-left:auto; color:#8B949E; font-size:12px; }
  .ok { color:#3FB950; font-weight:700; } .run { color:#D29922; font-weight:700; }
  .btn { display:inline-block; margin:12px 14px; padding:6px 14px; border-radius:6px;
    background:#238636; color:#fff; font-size:13px; font-weight:600; }
  .btn.done { background:#8957E5; }

  .sf { position:absolute; top:56px; left:24px; right:24px; bottom:20px; border-radius:10px;
    overflow:hidden; background:#FFFFFF; color:#1E252F;
    box-shadow:0 18px 50px rgba(0,0,0,.45), 0 0 0 1px rgba(232,244,250,.08); }
  .sf .bar { background:#E9EEF3; color:#4B5563; }
  .sf .rail { position:absolute; top:30px; left:0; bottom:0; width:52px; background:#F4F6F9;
    border-right:1px solid #E1E6EC; display:flex; flex-direction:column; align-items:center;
    gap:14px; padding-top:16px; }
  .sf .rail i { display:block; width:20px; height:20px; border-radius:6px; background:#D5DCE4; }
  .sf .rail i.on { background:#29B5E8; }
  .sf .top { position:absolute; top:30px; left:52px; right:0; height:46px; display:flex;
    align-items:center; gap:10px; padding:0 18px; border-bottom:1px solid #E1E6EC;
    font-size:13px; color:#5B6573; }
  .sf .top b { color:#1E252F; font-weight:600; }
  .sf .live { display:inline-flex; align-items:center; gap:6px; background:#E6F6EC;
    color:#1A7F37; border-radius:999px; padding:3px 10px; font-size:12px; font-weight:600; }
  .sf .live::before { content:""; width:7px; height:7px; border-radius:50%; background:#2DA44E; }
  .sf .acts { margin-left:auto; display:flex; gap:8px; }
  .sf .acts span { border:1px solid #D0D7DE; border-radius:6px; padding:4px 11px; color:#1E252F; }
  .sf .acts span.p { background:#29B5E8; border-color:#29B5E8; color:#fff; }
  .sf .frame { position:absolute; top:76px; left:52px; right:0; bottom:0; overflow:hidden; }
  .sf .frame img { width:100%; display:block; }
"""

# Anything that would identify a real account must never reach a frame.
_LEAKS = re.compile(
    r"/Users/|/home/|snowflakecomputing\.com|app\.snowflake\.com/"
    r"|\b[\w.+-]+@(?!example\.(?:com|org|net)\b)[\w-]+\.[\w.]+"
)


def check_clean(markup: str) -> None:
    """Refuse a scene that carries a home path, an account URL or a real email.

    Image sources are local file URIs that never appear on screen, so they are skipped.
    """
    hit = _LEAKS.search(re.sub(r'src="[^"]*"', "", markup))
    if hit:
        raise SystemExit(f"refusing to render a frame containing {hit.group(0)!r}")


# ---------- chat items ----------------------------------------------------------------


def user(text: str) -> str:
    return f'<div class="u">{_inline(text)}</div>'


def claude(markup: str) -> str:
    return f'<div class="c"><span class="mark">&#10043;</span><div>{markup}</div></div>'


def tools(rows: list[tuple[str, str, str]]) -> str:
    """rows: (state, kind, label) with state "ok" or "run"."""
    out = []
    for state, kind, label in rows:
        mark = "&#10003;" if state == "ok" else "&#9680;"
        out.append(
            f'<div class="tool"><span class="st {state}">{mark}</span>'
            f'<span class="k">{html.escape(kind)}</span>{html.escape(label)}</div>'
        )
    return f'<div class="tools">{"".join(out)}</div>'


def ask(header: str, question: str, options: list[str], pick: int | None = None) -> str:
    opts = "".join(
        f'<div class="o{" pick" if i == pick else ""}">'
        f"{'&#10003; ' if i == pick else ''}{html.escape(o)}</div>"
        for i, o in enumerate(options)
    )
    return (
        f'<div class="ask"><div class="h">{html.escape(header)}</div>'
        f'<div class="q">{html.escape(question)}</div>{opts}</div>'
    )


def wire(text: str) -> str:
    return f'<div class="wire">{html.escape(text)}</div>'


def _inline(text: str) -> str:
    """Escape text, rendering a leading /command as code."""
    m = re.match(r"(/[\w-]+)(.*)", text, re.S)
    if not m:
        return html.escape(text)
    return f"<code>{html.escape(m.group(1))}</code>{html.escape(m.group(2))}"


# ---------- frames --------------------------------------------------------------------


def caption(n: int | str, title: str, sub: str) -> str:
    return (
        f'<div class="caption"><span class="n">{n}</span>{title}'
        f'<span class="sub">{sub}</span></div>'
    )


def chat_frame(cap: str, feed: list[str], typed: str | None = None) -> str:
    if typed is None:
        composer = (
            '<span class="ph">Reply to Claude&hellip;</span><span class="send off">&uarr;</span>'
        )
    else:
        composer = (
            f'<span>{_inline(typed)}</span><span class="caret"></span>'
            '<span class="send">&uarr;</span>'
        )
    return (
        cap + '<div class="app"><div class="bar"><i></i><i></i><i></i>'
        f'<span class="t">{REPO_NAME}</span></div>'
        '<div class="side"><div class="new">+ New session</div><h6>Sessions</h6>'
        '<div class="s on">Sales dashboard</div><div class="s">Onboard StreamSnow</div>'
        '<div class="s">Fix region filter</div></div>'
        f'<div class="main"><div class="feed">{"".join(feed)}</div>'
        f'<div class="composer">{composer}</div></div></div>'
    )


def browser_frame(cap: str, img: Path, scroll: float, path: str) -> str:
    return (
        cap + '<div class="window browser"><div class="bar"><i></i><i></i><i></i>'
        f'<span class="url">127.0.0.1:8501/{path}</span></div>'
        f'<div class="viewport"><img src="{img.as_uri()}" '
        f'style="transform:translateY(-{scroll:.0f}px)"></div></div>'
    )


def pr_frame(cap: str, merged: bool, checks: str, deploy: list[tuple[str, str]]) -> str:
    """checks: "run" or "ok"; deploy: (state, step name) rows, empty before the merge."""
    pill = (
        '<span class="pill merged">&#10003; Merged</span>'
        if merged
        else '<span class="pill open">&#9675; Open</span>'
    )
    head = (
        '<span class="ok">&#10003;</span>All checks have passed'
        if checks == "ok"
        else '<span class="run">&#9680;</span>Some checks haven&rsquo;t completed yet'
    )
    mark = "&#10003;" if checks == "ok" else "&#9680;"
    rows = (
        f'<div class="row"><span class="{checks}">{mark}</span>'
        'checks / checks <span class="meta">(pull_request)</span>'
        f'<span class="d">{"Successful" if checks == "ok" else "In progress"}</span></div>'
    )
    button = (
        '<span class="btn done">Squash and merge &middot; done</span>'
        if merged
        else '<span class="btn">Squash and merge</span>'
    )
    deploy_box = ""
    if deploy:
        steps = "".join(
            f'<div class="row"><span class="{s}">{"&#10003;" if s == "ok" else "&#9680;"}</span>'
            f"{html.escape(name)}</div>"
            for s, name in deploy
        )
        done = all(s == "ok" for s, _ in deploy)
        title = "deploy &middot; on push to main" + (
            ' <span class="ok">&#10003; Success</span>' if done else ""
        )
        deploy_box = f'<div class="box"><div class="hd">{title}</div>{steps}</div>'
    return (
        cap + '<div class="gh"><div class="bar"><i></i><i></i><i></i>'
        f'<span class="url">github.com/acme/{REPO_NAME}/pull/1</span></div>'
        f'<div class="pr"><h2>{html.escape(PR_TITLE)} <span>#1</span></h2>{pill}'
        f'<span class="meta">sales-performance &rarr; main &middot; opened by /ship-app</span>'
        f'<div class="box"><div class="hd">{head}</div>{rows}'
        f"{'' if deploy else button}</div>{deploy_box}</div></div>"
    )


def snowflake_frame(cap: str, img: Path, scroll: float, frame_w: float) -> str:
    # Streamlit's own "Deploy" toolbar sits at the top of a local screenshot; it does not
    # exist in Snowflake, so the frame starts below it.
    offset = scroll + SHOT_TOOLBAR * frame_w / SHOT_W
    rail = '<i class="on"></i>' + "<i></i>" * 6
    return (
        cap + '<div class="sf"><div class="bar"><i></i><i></i><i></i>'
        '<span class="t">Snowsight</span></div>'
        f'<div class="rail">{rail}</div>'
        '<div class="top">Projects &rsaquo; Streamlit &rsaquo; '
        f'<b>{APP_OBJECT}</b><span class="live">Running</span>'
        '<span class="acts"><span>Edit</span><span class="p">Share</span></span></div>'
        f'<div class="frame"><img src="{img.as_uri()}" '
        f'style="transform:translateY(-{offset:.0f}px)"></div></div>'
    )


# ---------- the story -----------------------------------------------------------------


@dataclass
class Step:
    """One skill in the chat: a typed prompt, then items added (or replacing the last)."""

    n: int
    title: str
    sub: str
    prompt: str
    beats: list[tuple[str, str, float]] = field(default_factory=list)

    @property
    def cap(self) -> str:
        return caption(self.n, self.title, self.sub)


def _builders(state: str) -> str:
    return tools(
        [(state, "page-builder ", p) for p in ("Overview", "Regions", "Products", "Customers")]
    )


def _reviewers(state: str) -> str:
    names = ("SQL efficiency", "data/lineage", "UI patterns", "runtime/config", "docs/governance")
    return tools(
        [("ok", "validate-app ", "16 checks · PASS")] + [(state, "reviewer ", n) for n in names]
    )


WIREFRAME = """Overview          KPIs vs a year earlier · monthly revenue
Regions & markets revenue by region, country, segment
Products          brands · top part types · top 10 parts
Customers         top 20 customers · orders per customer
About             purpose · definitions · sources"""

STEPS = [
    Step(
        1,
        "/build-app",
        "an idea in, a governed app out, with your OK at each checkpoint",
        "/build-app Let's build a sales dashboard using the demo set.",
        [
            (
                "add",
                claude(
                    "<b>Phase 1 &middot; Spec.</b> TPC-H sample data, monthly "
                    "trend, comparisons a year earlier. Writing REQUIREMENTS.md."
                ),
                1.0,
            ),
            (
                "add",
                ask(
                    "Checkpoint 1",
                    "Confirm the spec and move on to data discovery and page design?",
                    ["Confirm (Recommended)", "Change something"],
                ),
                1.0,
            ),
            (
                "set",
                ask(
                    "Checkpoint 1",
                    "Confirm the spec and move on to data discovery and page design?",
                    ["Confirm (Recommended)", "Change something"],
                    0,
                ),
                0.7,
            ),
            (
                "add",
                tools(
                    [
                        ("run", "data-scout ", "profile TPCH_SF1"),
                        ("run", "app-designer ", "plan the pages"),
                    ]
                ),
                0.6,
            ),
            (
                "set",
                tools(
                    [
                        ("ok", "data-scout ", "profile TPCH_SF1"),
                        ("ok", "app-designer ", "plan 5 pages"),
                    ]
                ),
                0.6,
            ),
            ("add", claude("<b>Checkpoint 1b: the wireframe.</b> No code exists yet."), 0.4),
            ("add", wire(WIREFRAME), 1.6),
            (
                "add",
                ask(
                    "CP1b",
                    "Build the app from this wireframe?",
                    ["Build it (Recommended)", "Change it"],
                    0,
                ),
                0.8,
            ),
            ("add", _builders("run"), 0.9),
            ("set", _builders("ok"), 0.5),
            ("add", claude("All four builders came back with <b>every check clean</b>."), 1.2),
        ],
    ),
    Step(
        2,
        "/review-app",
        "the validate-app gate, then five reviewers in parallel",
        "/review-app sales-performance",
        [
            ("add", _reviewers("run"), 1.0),
            ("set", _reviewers("ok"), 0.5),
            (
                "add",
                claude(
                    "All five reviewers are back. <b>0 critical</b>, 3 should-fix, "
                    "all three fixed and committed. <code>validate-app</code> "
                    "still passes."
                ),
                2.0,
            ),
        ],
    ),
    Step(
        3,
        "/preview-app",
        "click through the reviewed app locally before shipping",
        "/preview-app sales-performance",
        [
            ("add", tools([("ok", "streamsnow preview ", "127.0.0.1:8501")]), 0.5),
            (
                "add",
                claude(
                    "The preview is running at <b>127.0.0.1:8501</b>. All five pages "
                    "loaded with zero console errors."
                ),
                1.2,
            ),
        ],
    ),
    Step(
        4,
        "/ship-app",
        "the PR, the checks, the merge, and CI deploys",
        "/ship-app sales-performance",
        [
            (
                "add",
                tools(
                    [
                        ("ok", "validate-app ", "PASS at HEAD · 0 critical open"),
                        ("ok", "git push ", "sales-performance"),
                        ("ok", "gh pr create ", "#1"),
                    ]
                ),
                0.8,
            ),
            (
                "add",
                claude(
                    f"The PR is open: <b>{REPO_NAME}#1</b>. Merging it deploys "
                    "the app to Snowflake through CI; nothing deploys from your "
                    "laptop."
                ),
                1.6,
            ),
        ],
    ),
]

DEPLOY_STEPS = [
    "Gate on configured secrets",
    "Install tooling",
    "Deploy changed apps (stage-copy)",
    "Reconcile tombstones",
    "Verify deploy health",
]


def eased(i: int, steps: int) -> float:
    return (1 - math.cos(math.pi * i / steps)) / 2
