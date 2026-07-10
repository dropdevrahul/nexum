"""dispatch.py — run one routed step in a git worktree via a chosen harness.

The keystone shared by ``/nx-build --harness`` (cross-harness offload) and the
companion TUI (managed agents). Given a single self-contained step, it:

  1. ensures a git worktree to run in (reuses worktree.create_worktree),
  2. builds an executor prompt from the step fields,
  3. runs the step in the chosen harness headless (harness.run),
  4. verifies the result with the existing guardrail.py (acceptance + scope),
  5. records an agents-registry row (+ step_ledger/usage when part of a plan),
  6. prints a verdict JSON of the same shape /nx-build already parses.

Everything is fail-open: any exception prints ``{"pass": false, "error": ...}``
and exits 0, so an orchestrator looping over steps never crashes on one bad
dispatch.

CLI:
    python3 dispatch.py --harness claude --model sonnet --repo <root> \
        (--worktree <path> | --new-worktree --slug <s>) --step-file <path> \
        [--session <id>] [--plan-hash <h>] [--index <n>] [--agent-id <id>]

step-file JSON:
    {"title": str, "objective": str, "contract": str,
     "scope_deny": [str], "acceptance": str, "files": [str]}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

# ---------------------------------------------------------------------------
# Bootstrap: make the scripts/ directory importable regardless of cwd
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402 — must be after sys.path tweak
import worktree  # noqa: E402
import harness  # noqa: E402


# ---------------------------------------------------------------------------
# Executor prompt
# ---------------------------------------------------------------------------

def _build_prompt(step: dict, worktree_path: str) -> str:
    """Compose the executor prompt from step fields.

    Mirrors the SHARED-CONTEXT + verbatim-step-fields shape used by
    commands/nx-build.md §5 and agents/nexum-impl-sonnet.md, so a headless
    harness gets the same instructions a Claude subagent would.
    """
    scope_deny = step.get("scope_deny") or []
    files = step.get("files") or []
    deny_line = ", ".join(scope_deny) if scope_deny else "none"
    files_line = ", ".join(files) if files else "none"
    return (
        "You are a nexum executor. Implement the step below in this working "
        f"directory ({worktree_path}). Touch ONLY the listed files; never touch "
        "any path in the scope deny-list. Make the acceptance command pass.\n\n"
        f"### {step.get('title', 'step')}\n"
        f"- files: {files_line}\n"
        f"- objective: {step.get('objective', '')}\n"
        f"- contract: {step.get('contract', '')}\n"
        f"- scope: do NOT touch {deny_line}\n"
        f"- acceptance: {step.get('acceptance', '')}\n"
    )


# ---------------------------------------------------------------------------
# git helpers (in the worktree)
# ---------------------------------------------------------------------------

def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, text=True, timeout=30,
    )


def _changed_files(worktree_path: str) -> list[str]:
    """Tracked-modified + untracked files in the worktree (relative paths)."""
    out: list[str] = []
    try:
        r = _git(worktree_path, "diff", "--name-only")
        if r.returncode == 0:
            out.extend(f for f in r.stdout.splitlines() if f.strip())
        r = _git(worktree_path, "ls-files", "--others", "--exclude-standard")
        if r.returncode == 0:
            out.extend(f for f in r.stdout.splitlines() if f.strip())
    except Exception:
        pass
    return out


def _diff(worktree_path: str) -> str:
    try:
        r = _git(worktree_path, "diff")
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# guardrail
# ---------------------------------------------------------------------------

def _run_guardrail(worktree_path: str, acceptance: str, scope_deny: list[str],
                   changed: list[str]) -> dict:
    """Invoke guardrail.py in the worktree; return its parsed JSON.

    guardrail runs the acceptance command with cwd = worktree so relative
    acceptance commands (e.g. `test -f fake_out.txt`) resolve against the
    checkout the harness just edited.
    """
    argv = [
        sys.executable, os.path.join(_SCRIPTS_DIR, "guardrail.py"),
        "--acceptance", acceptance or "",
        "--scope-root", worktree_path,
        "--changed", ",".join(changed),
    ]
    for deny in scope_deny:
        argv += ["--deny-path", deny]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                           cwd=worktree_path)
        return json.loads(r.stdout)
    except Exception as exc:
        return {"pass": False, "acceptance_rc": -1, "scope_violations": [],
                "log": f"[dispatch] guardrail invocation failed: {exc}"}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _handle(args) -> dict:
    agent_id = args.agent_id or uuid.uuid4().hex
    # realpath (not just abspath) so the stored repo_root matches what
    # `git rev-parse --show-toplevel` returns — on macOS /var is a symlink to
    # /private/var, and an unresolved abspath would fail the TUI's repo filter.
    repo = os.path.realpath(args.repo)

    # ---- 1. step file ----
    with open(args.step_file, "r", encoding="utf-8") as fh:
        step = json.load(fh)

    # ---- 2. worktree ----
    branch = None
    if args.new_worktree:
        cfg = store.get_config()
        worktree_path = worktree.create_worktree(
            repo, args.slug,
            cfg.get("worktree_copy", []),
            cfg.get("worktree_ignore", []),
        )
        if not worktree_path:
            return {"pass": False, "error": "worktree creation failed",
                    "agent_id": agent_id}
        branch = f"nexum/{args.slug}"
    else:
        worktree_path = args.worktree
        if not worktree_path or not os.path.isdir(worktree_path):
            return {"pass": False, "error": "worktree path missing or not a dir",
                    "agent_id": agent_id}

    # ---- 3. log path ----
    log_dir = os.path.join(repo, ".nexum-data", "agents")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{agent_id}.log")

    # ---- 4. register (running) ----
    # Record our own PID while the harness runs so `agent-list --active` (a live
    # os.kill(pid,0) probe) can distinguish a currently-executing agent from a
    # finished one. dispatch is synchronous, so when this process exits the pid
    # stops being alive and the row drops out of the active set on its own.
    store.record_agent(
        agent_id, harness=args.harness, model=args.model, repo_root=repo,
        worktree=worktree_path, branch=branch, log_path=log_path,
        task=step.get("title", ""), plan_hash=args.plan_hash,
        step_index=args.index, status="running", session_id=args.session,
        pid=os.getpid(), started_ts=time.time(),
    )

    # ---- 5-6. build prompt + run harness ----
    prompt = _build_prompt(step, worktree_path)
    hres = harness.run(args.harness, args.model, prompt, worktree_path, log_path)

    # ---- 7-9. verify + diff ----
    changed = _changed_files(worktree_path)
    guard = _run_guardrail(worktree_path, step.get("acceptance", ""),
                           step.get("scope_deny") or [], changed)
    diff = _diff(worktree_path)
    passed = bool(guard.get("pass"))

    # ---- 10. verdict ----
    verdict = {
        "pass": passed,
        "harness": args.harness,
        "model": args.model,
        "tokens": hres.get("tokens", 0),
        "cost_usd": hres.get("cost_usd", 0.0),
        "acceptance_rc": guard.get("acceptance_rc"),
        "scope_violations": guard.get("scope_violations", []),
        "diff": diff,
        "log": guard.get("log", ""),
        "agent_id": agent_id,
        "worktree": worktree_path,
    }

    # ---- 11. persist ----
    store.record_agent(agent_id, status=("done" if passed else "failed"),
                       cost_usd=hres.get("cost_usd", 0.0))
    if args.plan_hash and args.index is not None:
        store.record_step(
            args.session or "_nosession", args.plan_hash, args.index,
            status=("done" if passed else "failed"),
            title=step.get("title"), route="", tier_used=args.harness,
            last_diff=(None if passed else diff),
            verdict=json.dumps(verdict, sort_keys=True), attempts=1,
        )
    store.add_usage(args.session or "_nosession", args.model,
                    input_tok=0, output_tok=hres.get("tokens", 0))

    return verdict


def main() -> None:
    p = argparse.ArgumentParser(prog="dispatch.py",
                                description="Run one routed step in a worktree via a harness.")
    p.add_argument("--harness", required=True, choices=["claude", "opencode", "cursor"])
    p.add_argument("--model", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--worktree")
    p.add_argument("--new-worktree", action="store_true")
    p.add_argument("--slug")
    p.add_argument("--step-file", required=True)
    p.add_argument("--session")
    p.add_argument("--plan-hash")
    p.add_argument("--index", type=int)
    p.add_argument("--agent-id")
    args = p.parse_args()

    if args.new_worktree and not args.slug:
        print(json.dumps({"pass": False, "error": "--new-worktree requires --slug"}))
        sys.exit(0)

    try:
        result = _handle(args)
    except Exception as exc:
        result = {"pass": False, "error": str(exc)}
    print(json.dumps(result, sort_keys=True))
    sys.exit(0)


if __name__ == "__main__":
    main()
