# Agent TUI (crew)

**crew** is a terminal dashboard to run, chat with, and **manage coding agents**
across harnesses (Claude Code / OpenCode / Cursor) — each in its own git
worktree, without leaving your terminal. It ships in this repo under `tui/`
(Rust + [ratatui](https://ratatui.rs)).

!!! tip "Live screenshots"
    A page with real rendered frames, the feature grid, and a comparison table
    lives at **[the crew showcase](../crew/)**.

Each agent runs its harness's interactive REPL inside a **PTY that the dashboard
owns**, rendered in an embedded terminal pane. Select an agent and press `enter`
to drop into its terminal — you chat **inside the TUI**, side-by-side with the
agent list; `Ctrl-o` returns to the dashboard. No tmux, no window switching.

## Standalone — no backend required

crew runs on its own: it creates git worktrees **natively** (a `crew/<task>`
branch under `.crew/worktrees/`), keeps its own registry in `.crew/agents.json`,
and needs only `git` plus whichever agent CLI you launch (`claude`, `opencode`,
`cursor-agent`). From the dashboard you can review diffs, tail logs, and
**commit + push + open a PR** (`gh`) — all without any external service.

!!! note "Optional nexum engine"
    If the nexum Python engine sits alongside crew (`scripts/store.py`,
    `dispatch.py`), the header shows a `⚙ engine` badge and crew *additionally*
    surfaces the engine's **observed** plugin sessions and **headless delegated**
    agents, and enables `R` retry via `dispatch.py`. Without the engine those
    features simply don't appear — nothing breaks.

## Build & run

```bash
cd tui
cargo build --release          # binary → tui/target/release/crew

# from anywhere inside a git repo
crew                           # interactive dashboard
crew --new "fix the bug"       # create a worktree + run the harness headless in it
crew --new "quick" --here      # …or run in the current repo (no new worktree)
crew --doctor                  # check git / harness CLIs / gh / engine
crew --help
```

## Keys

| key | action |
|-----|--------|
| `j`/`k`, `↑`/`↓` | move · `g`/`G` top/bottom · `PgUp/Dn`, `Ctrl-u/d` page · `]`/`[` next/prev failed |
| `space` / `A` | mark row / mark all for bulk ops · `esc` clears |
| `1` `2` `3` `4` | filter: all / running / agents / sessions · `t` cycle sort |
| `n` | new agent (workflow / harness / model / worktree pickers); `Ctrl-S` launches |
| `enter` | chat with a live agent, or resume a `▷` persisted one |
| `shift-tab` | (in chat) forwarded to the agent — cycles Claude Code's modes |
| `d` / `l` | diff viewer / log tail (both `/`-searchable; log live-follows) |
| `e` / `!` | open the worktree in `$EDITOR` / drop to a `$SHELL` |
| `P` | commit + push the worktree (or all marked) and open a PR (`gh`) |
| `O` / `f` | open the branch/PR in a browser · `git fetch` |
| `R` | retry / re-delegate the task to a harness you pick |
| `s` / `S` / `x` / `c` | stop · stop all · remove (prunes clean worktrees) · clear finished |
| `L` / `o` / `y` / `Y` | label · show worktree path · yank path · yank branch |
| `,` / `?` | settings · help |

Row glyphs: `●` running · `✓` done · `✗` failed · `▷` resumable · `±` uncommitted
changes · `◉` marked.

## Workflows

The launcher's **workflow** picker controls how a task runs:

- `chat (single agent)` — plain interactive session.
- `plan → build` — seeds `/nx-plan` then `/nx-build` (needs the engine).
- `plan → build on cursor` / `… on opencode` — seeds `/nx-build --harness X`.
- `plan only` — seeds `/nx-plan` and stops for review.

## Delegation MCP

`scripts/mcp_server.py` is a stdio MCP server (registered in `.mcp.json` as
`nexum-delegate`) that lets one agent **delegate work to another harness** as a
managed sub-agent — `delegate`, `delegate_async`, `check`, `list_agents`. Because
the sub-agent is recorded in the `agents` table, it shows up **live in crew**
while it runs; select it to tail its log (`l`) or review its diff (`d`).

## How it compares

| capability | crew | claude-squad | lazygit | k9s |
|---|:--:|:--:|:--:|:--:|
| multiple agents, one view | ✓ | ✓ | ✓ | · |
| embedded chat (no tmux) | ✓ | — | — | — |
| per-agent git worktree | ✓ | ✓ | — | — |
| cross-harness | ✓ | · | — | — |
| diff viewer | ✓ | ✓ | ✓ | · |
| live log follow | ✓ | · | — | ✓ |
| commit · push · PR | ✓ | · | ✓ | — |
| multi-select bulk ops | ✓ | · | — | ✓ |
| runs standalone | ✓ | ✓ | ✓ | ✓ |
