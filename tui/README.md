# nexum-tui

A repo-scoped dashboard for running and **chatting with** coding agents across
harnesses (Claude / OpenCode / Cursor), Rust + [ratatui].

Each agent runs its harness's interactive REPL inside a **PTY that the dashboard
owns**, rendered in an **embedded terminal pane** (via `portable-pty` + `vt100` +
[tui-term]). Select an agent and press `enter` to drop into its terminal — you
chat **inside the TUI**, side-by-side with the agent list; `Ctrl-o` returns to the
dashboard. No tmux, no switching windows.

**Requires:** `python3` (the engine) and whichever harness CLIs you launch
(`claude`, `opencode`, `cursor-agent`).

**Quit & resume.** Live agents end when you quit (you get a confirmation first).
But each launch is persisted to `.nexum-data/tui-agents.json` and its git worktree
is kept, so on the next run the agent shows as **▷ resumable** — press `enter` to
relaunch the harness with its resume flag (`claude --continue`, `opencode
--continue`, `cursor-agent --resume`) in the same worktree, continuing the
conversation. Resume commands are overridable via `NEXUM_RESUME_CMD_<HARNESS>`.

[tui-term]: https://crates.io/crates/tui-term

It shows two classes of agent for the current repo:

- **managed** — agents nexum launched (rows in the `agents` table), with full
  control: stop, tail logs, reveal worktree.
- **observed** — normal plugin sessions writing to `nexum.db` (read-only).

The TUI never touches SQLite directly — it shells the Python engine
(`python3 <repo>/scripts/store.py … --json`, `dispatch.py …`), so `store.py`
stays the single source of truth for the schema.

## Build

```
cd tui
cargo build --release
```

The binary lands at `tui/target/release/nexum-tui`.

## Run

From anywhere inside a repo (it resolves the git toplevel and scopes to it):

```
nexum-tui                         # interactive dashboard
nexum-tui --new "fix the bug"     # launch an agent from the shell, print its session
nexum-tui --new "…" --harness opencode --model anthropic/claude-opus-4
nexum-tui --dump                  # print the agent list once and exit (no TTY)
nexum-tui --snapshot              # render one real frame to text (visual smoke, no TTY)
nexum-tui --scripts DIR           # override the scripts dir (default <repo>/scripts)
```

Environment:
- `NEXUM_SCRIPTS` — scripts dir (same as `--scripts`).
- `NEXUM_PYTHON` — python interpreter (default `python3`).
- `CLAUDE_PLUGIN_DATA` — inherited by the Python engine to pick the data dir / DB.

## Layout

A master-detail **explorer**: a selectable agent list on the left, a live
preview on the right.

```
┌ agents (2) ──────────┐┌ preview ─────────────────────────┐
│▶ ✓ cursor  add bill… ││ add billing dashboard endpoint   │
│  ● claude  fix auth… ││ status   ● running               │
│                      ││ harness  claude · sonnet         │
│                      ││ steps    1/2   cost $0.010       │
│                      ││ branch   nexum/billing           │
│                      │└──────────────────────────────────┘
│                      │┌ log (tail · [enter] full) ───────┐
│                      ││ {"type":"result","tokens":10,…}  │
└──────────────────────┘└──────────────────────────────────┘
```

## Features

- **Explorer view** — managed agents + observed sessions in a scrollable list,
  **priority-sorted** (live work first, then failed, then done, sessions last),
  colored status icons (`●` running, `✓` done, `✗` failed, `■` stopped), each row
  showing step `k/n` and relative time. Selecting one shows a full preview + live
  log tail on the right.
- **Diff/verdict viewer** (`d`) — review exactly what an offloaded agent changed:
  the recorded unified diff (colored) plus the guardrail verdict.
- **Live step progress** — plan-linked agents show `k/n` done steps from
  `step_ledger`.
- **Live PID liveness** — `a` filters to agents whose process is actually alive.
- **Header totals** — agent/live counts and summed metered cost.
- **Incremental search** (`/`) over task / harness / id / status.
- **Launcher** — `n` opens a form where the **task is required**; harness is a
  picker and the model is prefilled with that harness's default. Launches in a
  fresh git worktree.
- **Log tail** auto-follows; `enter` opens a full-screen scrollable log.
- **Stop** asks to confirm; **`o`** reveals the worktree path.
- Auto-refreshes every ~1.5s.

## Keys

| key | action |
|-----|--------|
| `j`/`k` or `↑`/`↓` | move selection |
| `n` | new agent form (multiline task, harness/model/worktree pickers, image attach); `Ctrl-S` to launch |
| `enter` | chat with a live agent, or **resume** a `▷` persisted one |
| `Ctrl-o` | leave the terminal, back to the dashboard |
| `q` | quit (confirms if agents are running) |
| `s` | stop the selected agent (`y` to confirm) |
| `o` | show the selected agent's worktree path |
| `x` | remove the selected agent |
| `/` | search · `h` toggle observed sessions |
| mouse wheel | scroll the agent list |

### New-agent form

- **task** — a multiline editor (Enter inserts a newline).
- **harness / model / worktree** — `‹ … ›` pickers, change with `←`/`→`.
  Worktree = *new isolated git worktree* or *current repo*.
- **images** — attach image paths (`a` add, `d` remove); appended to the prompt.
- **`Ctrl-S`** launches (Enter is reserved for the task editor).
| `/` | incremental search (`esc` clears, `enter` keeps) |
| `a` | toggle active-only (live PID) |
| `h` | toggle observed sessions |
| `r` | refresh now (auto every ~1.5s) |
| `?` | help overlay |
| `q` | quit (in log/help view, `q`/`esc` returns to the table) |

## Architecture

```
nexum-tui ──subprocess JSON──> python3 scripts/store.py agent-list/session-list --json
         ──spawn detached────> python3 scripts/dispatch.py --harness … --new-worktree …
```

`dispatch.py` (the keystone shared with `/nx-build --harness`) creates a git
worktree, runs the chosen harness headless, verifies with `guardrail.py`, and
records the `agents`/`step_ledger`/`usage` rows the TUI reads back.

## Future work

- Auto route→harness offload (pick the cheapest capable harness per plan step),
  fed by `route_calibration`.
- Interactive attach/takeover (tmux) for hands-on driving of a running agent.
- Launch a whole plan across a harness from the TUI (currently per-agent).

[ratatui]: https://ratatui.rs
