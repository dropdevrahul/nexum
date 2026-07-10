"""harness.py — headless-CLI adapters for claude / opencode / cursor.

Thin, fail-open layer that knows how to:
- build the argv for a headless agent CLI invocation (``build_command``)
- best-effort parse a stream of JSON-lines output into a small summary dict
  (``parse_stream``)
- spawn the process, stream its combined stdout+stderr to a log file, and
  return the parsed result (``run``)

No worktree/guardrail/dispatch logic lives here — this module only knows how
to talk to the three headless CLIs. A later module composes this with
worktree.py and guardrail.py to actually execute a step.

Fail-open: ``run`` never raises. A missing binary (OSError) or a timeout
(subprocess.TimeoutExpired) is reported as ``{"ok": False, "status": "error", ...}``
so a caller looping over agents never crashes on one bad process.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any, Dict, Iterable, List

# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------

_KNOWN_HARNESSES = ("claude", "opencode", "cursor")


def build_command(harness: str, model: str, prompt: str, cwd: str) -> List[str]:
    """Return the argv to invoke *harness* headlessly with *model* and *prompt*.

    ``cwd`` is accepted for signature symmetry with ``run`` (some harnesses'
    argv doesn't need it — it's passed as the subprocess working directory
    instead) but is not currently baked into the argv itself.

    If env var ``NEXUM_HARNESS_CMD_<HARNESS>`` (harness name upper-cased) is
    set, it is shlex-split as the base argv and *prompt* is appended as the
    final argument — this lets tests (and users) override the binary/args
    without touching this module.

    Raises ValueError for an unrecognized harness name.
    """
    override = os.environ.get(f"NEXUM_HARNESS_CMD_{harness.upper()}", "").strip()
    if override:
        return shlex.split(override) + [prompt]

    if harness == "claude":
        return ["claude", "-p", prompt, "--output-format", "stream-json", "--model", model]
    if harness == "opencode":
        return ["opencode", "run", prompt, "--model", model, "--format", "json"]
    if harness == "cursor":
        return ["cursor-agent", "-p", prompt, "--output-format", "stream-json", "--model", model]

    raise ValueError(f"unknown harness: {harness!r} (expected one of {_KNOWN_HARNESSES})")


# ---------------------------------------------------------------------------
# parse_stream
# ---------------------------------------------------------------------------

def parse_stream(harness: str, lines: Iterable[str]) -> Dict[str, Any]:
    """Best-effort parse a stream of JSON-lines into a summary dict.

    Returns ``{"status": "done"|"error"|"running", "final_text": str,
    "tokens": int, "cost_usd": float}``. Non-JSON / unrecognized lines are
    ignored. Missing fields default (tokens 0, cost_usd 0.0, final_text "").
    A line whose parsed ``type`` is ``"result"`` sets status to ``"done"`` and
    pulls tokens/cost from it when present.
    """
    status = "running"
    final_text = ""
    tokens = 0
    cost_usd = 0.0

    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue

        obj_type = obj.get("type")
        if obj_type == "result":
            status = "error" if obj.get("is_error") else "done"
            if "tokens" in obj:
                try:
                    tokens = int(obj.get("tokens") or 0)
                except (TypeError, ValueError):
                    pass
            if "cost_usd" in obj:
                try:
                    cost_usd = float(obj.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
            text = obj.get("final_text") or obj.get("result") or obj.get("text")
            if isinstance(text, str) and text:
                final_text = text
        elif obj_type == "error":
            status = "error"

    return {
        "status": status,
        "final_text": final_text,
        "tokens": tokens,
        "cost_usd": cost_usd,
    }


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def run(
    harness: str,
    model: str,
    prompt: str,
    cwd: str,
    log_path: str,
    timeout: int = 1800,
) -> Dict[str, Any]:
    """Spawn the headless agent CLI, stream output to *log_path*, return a
    result dict.

    Combines stdout+stderr, appends each line to *log_path* as it arrives (the
    log file is opened in append mode so multiple runs sharing a log don't
    clobber each other), and returns
    ``{**parse_stream(...), "ok": bool, "rc": int, "log_path": log_path}``.
    ``ok`` is True iff the process exit code is 0.

    Fail-open: if the binary is missing (OSError) or the process exceeds
    *timeout* (subprocess.TimeoutExpired), returns
    ``{"ok": False, "status": "error", "final_text": "", "tokens": 0,
    "cost_usd": 0.0, "rc": -1, "log_path": log_path}`` — never raises.
    """
    try:
        argv = build_command(harness, model, prompt, cwd)
    except ValueError:
        return {
            "ok": False,
            "status": "error",
            "final_text": "",
            "tokens": 0,
            "cost_usd": 0.0,
            "rc": -1,
            "log_path": log_path,
        }

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "ok": False,
            "status": "error",
            "final_text": "",
            "tokens": 0,
            "cost_usd": 0.0,
            "rc": -1,
            "log_path": log_path,
        }

    combined = (proc.stdout or "") + (proc.stderr or "")
    lines = combined.splitlines()

    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
    except OSError:
        pass  # logging is best-effort; never fail the run over it

    result = parse_stream(harness, lines)
    result["ok"] = proc.returncode == 0
    result["rc"] = proc.returncode
    result["log_path"] = log_path
    return result


# ---------------------------------------------------------------------------
# self-check
# ---------------------------------------------------------------------------

def _demo() -> None:
    """Self-check: build_command shapes, parse_stream, and a fail-open run
    against a nonexistent binary. Run: python3 scripts/harness.py"""
    cmd = build_command("claude", "sonnet", "hello", "/tmp")
    assert cmd == ["claude", "-p", "hello", "--output-format", "stream-json", "--model", "sonnet"], cmd

    parsed = parse_stream("claude", ['{"type":"result","tokens":5,"cost_usd":0.0}'])
    assert parsed["status"] == "done" and parsed["tokens"] == 5, parsed

    os.environ["NEXUM_HARNESS_CMD_CLAUDE"] = "/nonexistent/nexum-demo-binary --flag"
    try:
        result = run("claude", "sonnet", "hi", "/tmp", "/tmp/nexum-harness-demo.log")
        assert result["ok"] is False and result["status"] == "error", result
    finally:
        os.environ.pop("NEXUM_HARNESS_CMD_CLAUDE", None)

    print("harness demo OK")


if __name__ == "__main__":
    _demo()
