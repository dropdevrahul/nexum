#!/usr/bin/env python3
"""gen.py — build the crew GitHub Pages site (docs/index.html).

Captures live terminal frames from the built `crew` binary (`--snapshot` in
several modes), escapes them into styled faux-terminal panes, and writes a
self-contained static page. Reproducible: re-run after UI changes to refresh the
screenshots.

    cd tui && cargo build && python3 site/gen.py
"""
from __future__ import annotations

import html
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TUI = os.path.dirname(HERE)
REPO = os.path.dirname(TUI)
BIN = os.path.join(TUI, "target", "release", "crew")
if not os.path.exists(BIN):
    BIN = os.path.join(TUI, "target", "debug", "crew")
# Published as a static page inside the repo's mkdocs site (docs_dir=docs/), so
# it coexists with the markdown docs and lands at <site>/crew/.
OUT = os.path.join(REPO, "docs", "crew", "index.html")


def snap(mode: str | None) -> str:
    """Capture one --snapshot frame in the given NEXUM_SNAPSHOT_MODE."""
    env = os.environ.copy()
    if mode:
        env["NEXUM_SNAPSHOT_MODE"] = mode
    if mode in ("term",):
        env["NEXUM_INTERACTIVE_CMD_CLAUDE"] = "cat"
    try:
        out = subprocess.run([BIN, "--snapshot"], capture_output=True, text=True,
                             env=env, timeout=30).stdout
    except Exception as exc:  # never fail the build over a snapshot
        out = f"(snapshot unavailable: {exc})"
    return out.rstrip("\n")


def pane(title: str, body: str) -> str:
    dots = '<span class="d r"></span><span class="d y"></span><span class="d g"></span>'
    return (f'<figure class="term"><figcaption>{dots}<span class="t">{html.escape(title)}'
            f'</span></figcaption><pre>{html.escape(body)}</pre></figure>')


FEATURES = [
    ("🖥️", "Agents inside the TUI", "Every agent runs its harness REPL in a PTY rendered in an embedded pane. Select one, press enter, chat side-by-side. No tmux, no window juggling."),
    ("🌿", "Isolated worktrees", "Each agent runs in its own git worktree on a <code>crew/&lt;task&gt;</code> branch — created natively, no external tooling. Work never tangles."),
    ("🔀", "Any harness", "Claude Code, OpenCode, Cursor — launch, resume, and manage them side by side, each with its own model."),
    ("🔎", "Diff &amp; log viewers", "Review a worktree diff (colored, searchable) or tail a log that live-follows like <code>tail -f</code>. Scrollbars, <code>/</code> search, <code>n/N</code>."),
    ("🚀", "Ship from the TUI", "<code>P</code> commits, pushes the branch, and opens a PR with <code>gh</code> — straight from the dashboard, lazygit-style."),
    ("✔️", "Manage the fleet", "Multi-select with <code>space</code>, bulk stop/remove, quick-filters <code>1-4</code>, sort by status/cost/recent, jump between failed with <code>]</code>/<code>[</code>."),
]

# feature parity — ✓ has it, · partial, — none
COMPARE = [
    ("Multiple agents in one view", "✓", "✓", "✓", "·"),
    ("Embedded chat (no tmux)", "✓", "—", "—", "—"),
    ("Per-agent git worktree", "✓", "✓", "—", "—"),
    ("Cross-harness (Claude/Cursor/OpenCode)", "✓", "·", "—", "—"),
    ("Diff viewer", "✓", "✓", "✓", "·"),
    ("Live log follow", "✓", "·", "—", "✓"),
    ("Commit · push · PR", "✓", "·", "✓", "—"),
    ("Multi-select bulk actions", "✓", "·", "—", "✓"),
    ("Runs standalone (no backend)", "✓", "✓", "✓", "✓"),
]
COMPARE_COLS = ["crew", "claude-squad", "lazygit", "k9s"]


def build() -> str:
    hero = pane("crew · dashboard", snap("demo"))
    shots = [
        pane("launch — workflow picker", snap("new")),
        pane("embedded chat", snap("term")),
        pane("grouped help (?)", snap("help")),
    ]
    fcards = "".join(
        f'<div class="card"><div class="ic">{i}</div><h3>{t}</h3><p>{d}</p></div>'
        for i, t, d in FEATURES)

    head = "".join(f"<th>{c}</th>" for c in COMPARE_COLS)
    rows = ""
    for row in COMPARE:
        label, *cells = row
        tds = ""
        for i, c in enumerate(cells):
            cls = {"✓": "yes", "·": "part", "—": "no"}.get(c, "")
            own = " own" if i == 0 else ""
            tds += f'<td class="{cls}{own}">{c}</td>'
        rows += f"<tr><td class=lbl>{label}</td>{tds}</tr>"

    gallery = "".join(f'<div class="shot">{s}</div>' for s in shots)

    return TEMPLATE.format(hero=hero, features=fcards, chead=head, crows=rows, gallery=gallery)


TEMPLATE = """<!doctype html>
<html lang=en data-theme=dark>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>crew — a terminal dashboard for coding agents</title>
<meta name=description content="crew runs, chats with, and manages coding agents (Claude, OpenCode, Cursor) across git worktrees — from one terminal dashboard.">
<style>
:root{{--bg:#0b0e14;--panel:#0d1117;--ink:#e6edf3;--dim:#8b949e;--line:#232a34;--acc:#39d0d8;--acc2:#7c5cff;--green:#3fb950;--red:#f85149;--yellow:#e3b341}}
*{{box-sizing:border-box}}
body{{margin:0;background:radial-gradient(1200px 600px at 70% -10%,#16202e 0,var(--bg) 60%);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:0 20px}}
a{{color:var(--acc)}}
header.top{{padding:16px 0;display:flex;align-items:center;gap:12px}}
.logo{{font-weight:800;font-size:20px;letter-spacing:.5px}}
.logo b{{background:var(--acc2);color:#fff;padding:2px 8px;border-radius:6px}}
.nav{{margin-left:auto;display:flex;gap:18px;font-size:14px}}
.nav a{{color:var(--dim);text-decoration:none}}
.hero{{text-align:center;padding:48px 0 12px}}
.hero h1{{font-size:clamp(30px,6vw,54px);line-height:1.05;margin:.2em 0}}
.hero h1 .g{{background:linear-gradient(90deg,var(--acc),var(--acc2));-webkit-background-clip:text;background-clip:text;color:transparent}}
.hero p{{font-size:19px;color:var(--dim);max-width:640px;margin:12px auto 24px}}
.cta{{display:inline-flex;gap:12px;flex-wrap:wrap;justify-content:center}}
.btn{{display:inline-block;padding:11px 20px;border-radius:9px;font-weight:600;text-decoration:none}}
.btn.p{{background:var(--acc);color:#04121a}}
.btn.s{{border:1px solid var(--line);color:var(--ink)}}
.term{{margin:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--panel);box-shadow:0 24px 60px -30px #000}}
.term figcaption{{display:flex;align-items:center;gap:8px;padding:9px 12px;background:#11161f;border-bottom:1px solid var(--line);font-size:12px;color:var(--dim)}}
.term .d{{width:11px;height:11px;border-radius:50%;display:inline-block}}
.term .d.r{{background:#ff5f56}}.term .d.y{{background:#ffbd2e}}.term .d.g{{background:#27c93f}}
.term .t{{margin-left:6px}}
.term pre{{margin:0;padding:16px;overflow-x:auto;font:13px/1.32 ui-monospace,"SF Mono",Menlo,Consolas,monospace;color:#cdd9e5;white-space:pre}}
.heroshot{{margin:26px 0 8px}}
section{{padding:40px 0}}
h2{{font-size:28px;text-align:center;margin:0 0 6px}}
.sub{{text-align:center;color:var(--dim);margin:0 0 28px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px}}
.card .ic{{font-size:24px}}
.card h3{{margin:.5em 0 .3em;font-size:17px}}
.card p{{margin:0;color:var(--dim);font-size:14.5px}}
.card code,td code{{background:#1b2230;padding:1px 5px;border-radius:5px;font-size:.9em;color:#b7c5d3}}
table{{width:100%;border-collapse:collapse;font-size:14.5px;overflow:hidden;border-radius:12px;border:1px solid var(--line)}}
th,td{{padding:11px 12px;text-align:center;border-bottom:1px solid var(--line)}}
th{{background:#11161f;font-size:13px;color:var(--dim);font-weight:600}}
th:first-child,td.lbl{{text-align:left}}
td.lbl{{color:var(--ink)}}
th:nth-child(2){{color:var(--acc)}}
td.own{{background:#0f1a22}}
td.yes{{color:var(--green);font-weight:700}}
td.part{{color:var(--yellow)}}
td.no{{color:#4b5563}}
.gallery{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
.shot .term pre{{font-size:11px;line-height:1.28}}
pre.code{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto;font:13.5px/1.6 ui-monospace,Menlo,monospace;color:#cdd9e5}}
.callout{{border:1px solid var(--line);border-left:3px solid var(--acc2);background:#0f1320;border-radius:10px;padding:16px 18px;color:var(--dim)}}
footer{{text-align:center;color:var(--dim);padding:40px 0;font-size:13px;border-top:1px solid var(--line);margin-top:20px}}
@media(max-width:760px){{.gallery{{grid-template-columns:1fr}}.nav{{display:none}}}}
</style>
</head>
<body>
<div class=wrap>
<header class=top>
  <span class=logo><b>crew</b></span>
  <nav class=nav>
    <a href=#features>Features</a><a href=#compare>Compare</a><a href=#shots>Screens</a><a href=#install>Install</a>
  </nav>
</header>

<section class=hero>
  <h1>Run a <span class=g>crew of coding agents</span><br>from one terminal.</h1>
  <p>crew launches, chats with, and manages Claude&nbsp;Code, OpenCode &amp; Cursor agents — each in its own git worktree — without leaving your terminal. Standalone Rust + ratatui.</p>
  <div class=cta>
    <a class="btn p" href=#install>Get started</a>
    <a class="btn s" href=#features>See features</a>
  </div>
  <div class=heroshot>{hero}</div>
</section>

<section id=features>
  <h2>Everything, one keystroke away</h2>
  <p class=sub>A master–detail dashboard that treats agents like a first-class resource.</p>
  <div class=grid>{features}</div>
</section>

<section id=compare>
  <h2>How it compares</h2>
  <p class=sub>Inspired by the best terminal tools — built for agents.</p>
  <table>
    <thead><tr><th>Capability</th>{chead}</tr></thead>
    <tbody>{crows}</tbody>
  </table>
</section>

<section id=shots>
  <h2>Screens</h2>
  <p class=sub>Real frames rendered by the binary.</p>
  <div class=gallery>{gallery}</div>
</section>

<section id=install>
  <h2>Install &amp; run</h2>
  <p class=sub>One binary. No backend required.</p>
  <pre class=code># build
cd tui &amp;&amp; cargo build --release

# run from anywhere inside a git repo
./target/release/crew

# …or launch headless from the shell (creates a worktree, runs the harness)
./target/release/crew --new "add billing endpoint" --harness cursor

# in the dashboard: [n] launch · [enter] chat · [P] push+PR · [?] keymap</pre>
  <p></p>
  <div class=callout><b>Standalone by default.</b> crew creates worktrees natively and keeps its own registry under <code>.crew/</code> — it needs only <code>git</code> and whichever agent CLI you launch. Drop in the optional nexum engine and it also surfaces observed sessions and headless delegation.</div>
</section>

<footer>
  crew — a terminal dashboard for coding agents · Rust + <a href="https://ratatui.rs">ratatui</a>
</footer>
</div>
</body>
</html>
"""


def main() -> None:
    if not os.path.exists(BIN):
        print(f"crew binary not found at {BIN}; run `cargo build` first", file=sys.stderr)
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(build())
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
