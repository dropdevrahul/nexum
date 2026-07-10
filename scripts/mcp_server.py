"""mcp_server.py — an MCP (stdio) server that lets one agent delegate work to
another harness as a managed sub-agent.

Claude (or any MCP client) calls the ``delegate`` tool with a task and a target
harness (``cursor`` / ``opencode`` / ``claude``). The server runs the existing
``dispatch.py`` — which creates a git worktree, runs that harness headless,
verifies with guardrail.py, and records an ``agents`` row — then returns the
verdict (pass, diff, cost, log tail). Because dispatch records the agent row,
the delegated sub-agent shows up **live in the nexum TUI** while it runs.

It's synchronous: one ``delegate`` call blocks until the sub-agent finishes and
returns its verdict. A client that wants several at once just emits several
tool calls in one turn (they run concurrently in the client). ``list_agents``
returns the current managed agents for the repo (the TUI's data), so the caller
can see siblings it (or the user) launched.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio). No
third-party deps — stdlib only, matching the rest of scripts/. Fail-open: a bad
request yields a JSON-RPC error, never a crash.

Register in a repo's .mcp.json:
    {"mcpServers": {"nexum-delegate": {
        "command": "python3", "args": ["scripts/mcp_server.py"]}}}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_DISPATCH = os.path.join(_SCRIPTS_DIR, "dispatch.py")
_STORE = os.path.join(_SCRIPTS_DIR, "store.py")

_HARNESSES = ("claude", "opencode", "cursor")
# Default model per harness when the caller doesn't pick one (mirrors the TUI).
_DEFAULT_MODEL = {
    "claude": "sonnet",
    "opencode": "anthropic/claude-sonnet-4-6",
    "cursor": "auto",
}
# Cap the diff we hand back so a huge change can't blow the caller's context.
_DIFF_CAP = 6000

PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    """git toplevel of the server's cwd (the project MCP was launched from)."""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return os.path.realpath(r.stdout.strip())
    except Exception:
        pass
    return os.path.realpath(os.getcwd())


def _slug(task: str) -> str:
    base = "".join(c.lower() if c.isalnum() else "-" for c in task)
    base = "-".join(p for p in base.split("-") if p)[:20]
    ts = int(time.time() * 1000) % 1_000_000
    return f"{base}-{ts}" if base else f"delegate-{ts}"


def _build_step(args: dict) -> dict:
    task = (args.get("task") or "").strip()
    return {
        "title": task[:80],
        "objective": task,
        "contract": "",
        "scope_deny": [],
        "acceptance": args.get("acceptance") or "",
        "files": args.get("files") or [],
    }


def _validate(args: dict):
    """Return (harness, task, model) or an error string."""
    harness = args.get("harness")
    task = (args.get("task") or "").strip()
    if harness not in _HARNESSES:
        return f"error: harness must be one of {', '.join(_HARNESSES)}"
    if not task:
        return "error: task is required"
    return harness, task, (args.get("model") or _DEFAULT_MODEL[harness])


def _git_diff(worktree: str) -> str:
    """Best-effort `git diff` of a worktree, capped, for reporting a verdict."""
    if not worktree or not os.path.isdir(worktree):
        return ""
    try:
        r = subprocess.run(["git", "-C", worktree, "diff"],
                           capture_output=True, text=True, timeout=30)
        diff = r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""
    if len(diff) > _DIFF_CAP:
        diff = diff[:_DIFF_CAP] + f"\n… [diff truncated at {_DIFF_CAP} chars]"
    return diff


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "delegate",
        "description": (
            "Delegate a self-contained task to another coding-agent harness "
            "(cursor / opencode / claude) as a managed sub-agent. Runs it "
            "headless in an isolated git worktree, verifies with the acceptance "
            "command if given, and returns the verdict (pass, diff, cost). The "
            "sub-agent appears live in the nexum TUI while it runs. Blocks until "
            "it finishes. Give ONE bounded task with all context inline — the "
            "sub-agent shares none of your conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "harness": {"type": "string", "enum": list(_HARNESSES),
                            "description": "Target harness to run the task."},
                "task": {"type": "string",
                         "description": "Full self-contained task/objective."},
                "model": {"type": "string",
                          "description": "Model override; omit for the harness default."},
                "acceptance": {"type": "string",
                               "description": "Shell command that must exit 0 for the "
                               "delegation to count as passed (run in the worktree). "
                               "Omit for no verification."},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "Files the sub-agent is expected to touch (scope hint)."},
            },
            "required": ["harness", "task"],
        },
    },
    {
        "name": "delegate_async",
        "description": (
            "Like delegate, but returns immediately with an agent_id instead of "
            "waiting. Use to fan out several sub-agents in parallel without "
            "holding this turn open — then poll each with `check`. The sub-agent "
            "runs in the background and shows live in the nexum TUI."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "harness": {"type": "string", "enum": list(_HARNESSES)},
                "task": {"type": "string"},
                "model": {"type": "string"},
                "acceptance": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["harness", "task"],
        },
    },
    {
        "name": "check",
        "description": (
            "Check a delegated (async) sub-agent by agent_id: status "
            "(running/done/failed), cost, worktree, and — once finished — its "
            "diff. Poll this after delegate_async."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
    {
        "name": "list_agents",
        "description": (
            "List managed sub-agents for this repo (the nexum TUI's data): id, "
            "harness, model, status, task, cost. Use to see what you or the user "
            "have delegated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean",
                                "description": "Only agents whose process is still alive."},
            },
        },
    },
]


def _tool_delegate(args: dict) -> str:
    v = _validate(args)
    if isinstance(v, str):
        return v
    harness, task, model = v

    step = _build_step(args)
    repo = _repo_root()
    fd, step_path = tempfile.mkstemp(prefix="nexum-step-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(step, fh)
        argv = [
            sys.executable, _DISPATCH,
            "--harness", harness, "--model", model, "--repo", repo,
            "--new-worktree", "--slug", _slug(task),
            "--step-file", step_path,
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=2000)
        except subprocess.TimeoutExpired:
            return "error: delegation timed out (>2000s)"
        verdict = _parse_last_json(proc.stdout)
        if verdict is None:
            return f"error: dispatch produced no verdict.\nstderr: {proc.stderr[:1000]}"
    finally:
        try:
            os.unlink(step_path)
        except OSError:
            pass

    diff = verdict.get("diff") or ""
    if len(diff) > _DIFF_CAP:
        diff = diff[:_DIFF_CAP] + f"\n… [diff truncated at {_DIFF_CAP} chars]"
    summary = {
        "pass": verdict.get("pass"),
        "harness": verdict.get("harness"),
        "model": verdict.get("model"),
        "agent_id": verdict.get("agent_id"),
        "worktree": verdict.get("worktree"),
        "cost_usd": verdict.get("cost_usd"),
        "acceptance_rc": verdict.get("acceptance_rc"),
        "scope_violations": verdict.get("scope_violations"),
        "diff": diff,
        "log_tail": (verdict.get("log") or "")[-1200:],
    }
    return json.dumps(summary, indent=2)


def _tool_delegate_async(args: dict) -> str:
    """Kick off a delegation without waiting. Returns an agent_id to poll with
    `check`. Lets one orchestrator fan out several sub-agents in parallel."""
    v = _validate(args)
    if isinstance(v, str):
        return v
    harness, task, model = v

    repo = _repo_root()
    agent_id = uuid.uuid4().hex
    # Persist the step file where the detached process can read it after we
    # return (a small leak by design — kept as a record of what was dispatched).
    steps_dir = os.path.join(repo, ".nexum-data", "steps")
    os.makedirs(steps_dir, exist_ok=True)
    step_path = os.path.join(steps_dir, f"{agent_id}.json")
    with open(step_path, "w", encoding="utf-8") as fh:
        json.dump(_build_step(args), fh)

    argv = [
        sys.executable, _DISPATCH,
        "--harness", harness, "--model", model, "--repo", repo,
        "--new-worktree", "--slug", _slug(task),
        "--step-file", step_path, "--agent-id", agent_id,
    ]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        start_new_session=True)  # detach: survives this call
    except Exception as exc:
        return f"error: could not launch delegation: {exc}"
    return json.dumps({
        "agent_id": agent_id, "harness": harness, "model": model,
        "status": "running",
        "note": "dispatched — poll with check(agent_id) for status/diff.",
    }, indent=2)


def _tool_check(args: dict) -> str:
    """Report the current state of a delegated agent (from the store), with its
    diff once it has finished."""
    agent_id = (args.get("agent_id") or "").strip()
    if not agent_id:
        return "error: agent_id is required"
    try:
        r = subprocess.run([sys.executable, _STORE, "agent-get", "--id", agent_id],
                           capture_output=True, text=True, timeout=30)
        row = json.loads(r.stdout or "null")
    except Exception as exc:
        return f"error: could not read agent: {exc}"
    if not row:
        return json.dumps({"agent_id": agent_id, "status": "unknown",
                           "note": "no such agent (not dispatched, or pruned)."})
    status = row.get("status")
    out = {
        "agent_id": agent_id,
        "status": status,
        "harness": row.get("harness"),
        "model": row.get("model"),
        "cost_usd": row.get("cost_usd"),
        "worktree": row.get("worktree"),
        "branch": row.get("branch"),
        "done": status in ("done", "failed"),
        "pass": status == "done" if status in ("done", "failed") else None,
    }
    if out["done"]:
        out["diff"] = _git_diff(row.get("worktree") or "")
    return json.dumps(out, indent=2)


def _tool_list_agents(args: dict) -> str:
    argv = [sys.executable, _STORE, "agent-list", "--repo", _repo_root(), "--json"]
    if args.get("active_only"):
        argv.append("--active")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        rows = json.loads(r.stdout or "[]")
    except Exception as exc:
        return f"error: could not list agents: {exc}"
    slim = [{
        "id": a.get("agent_id"),
        "harness": a.get("harness"),
        "model": a.get("model"),
        "status": a.get("status"),
        "task": a.get("task"),
        "cost_usd": a.get("cost_usd"),
        "branch": a.get("branch"),
        "worktree": a.get("worktree"),
    } for a in rows]
    return json.dumps(slim, indent=2)


def _parse_last_json(text: str):
    """dispatch prints one verdict JSON line; return the last parseable object."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


_DISPATCH_TOOLS = {
    "delegate": _tool_delegate,
    "delegate_async": _tool_delegate_async,
    "check": _tool_check,
    "list_agents": _tool_list_agents,
}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP dispatch
# ---------------------------------------------------------------------------

def handle_message(msg: dict):
    """Handle one JSON-RPC request; return a reply dict, or None for a
    notification (no reply). Pure — no I/O — so it's unit-testable."""
    method = msg.get("method")
    mid = msg.get("id")

    # notifications (no id / notifications/*): never reply
    if method and method.startswith("notifications/"):
        return None

    if method == "initialize":
        params = msg.get("params") or {}
        return _ok(mid, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "nexum-delegate", "version": "0.1.0"},
        })

    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        fn = _DISPATCH_TOOLS.get(name)
        if fn is None:
            return _err(mid, -32602, f"unknown tool: {name}")
        try:
            text = fn(params.get("arguments") or {})
        except Exception as exc:  # fail-open: report as tool error, not a crash
            return _ok(mid, {"content": [{"type": "text", "text": f"error: {exc}"}],
                            "isError": True})
        return _ok(mid, {"content": [{"type": "text", "text": text}]})

    if mid is None:
        return None  # unknown notification
    return _err(mid, -32601, f"method not found: {method}")


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve() -> None:
    """Read newline-delimited JSON-RPC from stdin, write replies to stdout."""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle_message(msg)
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()


# ---------------------------------------------------------------------------
# self-check
# ---------------------------------------------------------------------------

def _demo() -> None:
    """Run: python3 scripts/mcp_server.py --selftest"""
    init = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["serverInfo"]["name"] == "nexum-delegate", init

    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    lst = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in lst["result"]["tools"]}
    assert names == {"delegate", "delegate_async", "check", "list_agents"}, names

    bad = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "nope", "arguments": {}}})
    assert "error" in bad, bad

    # delegate validates args before running anything
    r = handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "delegate",
                                   "arguments": {"harness": "bogus", "task": "x"}}})
    assert "harness must be one of" in r["result"]["content"][0]["text"], r

    assert _parse_last_json('noise\n{"pass": true}\n') == {"pass": True}
    print("mcp_server demo OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _demo()
    else:
        serve()
