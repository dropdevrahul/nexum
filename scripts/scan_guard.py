"""
scan_guard.py — Nexum PreToolUse hook.

Detects context-blowing scans (unscoped recursive greps, broad globs, reads
into deny paths, etc.) and emits a deny decision or a narrowed updatedInput.

Hook contract:
  stdin  → single JSON object (Claude Code PreToolUse payload)
  stdout → single JSON object (deny shape, updatedInput shape, or {})
  exit 0 always (fail-open)
"""

import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# sys.path: ensure scripts/ dir is importable as "import store"
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402  (stdlib-only; store.py is in the same dir)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _deny(reason: str) -> None:
    """Emit a PreToolUse deny decision and exit 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"[nexum] {reason} — scope the search to a directory "
                "or add -maxdepth/path."
            ),
        }
    }
    print(json.dumps(out, sort_keys=True))
    sys.exit(0)


def _update_input(new_command: str) -> None:
    """Emit an updatedInput to narrow a Bash command."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {"command": new_command},
        }
    }
    print(json.dumps(out, sort_keys=True))
    sys.exit(0)


def _allow() -> None:
    """Emit {} (allow, no modification)."""
    print("{}")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Path-under-deny helper
# ---------------------------------------------------------------------------

def _under_deny(path: str, deny_paths: list) -> bool:
    """Return True if *path* starts with any deny entry (by path component)."""
    # Normalise: strip a leading "./" prefix, then any leading slashes.
    # NOTE: use slicing, not lstrip("./") — lstrip strips a *character set* and
    # would mangle dot-leading paths (".git" -> "git", so ".git/x" misses the
    # ".git" deny entry).
    p = path
    if p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    for entry in deny_paths:
        entry = entry.strip("/")
        if not entry:
            continue
        # Match if path equals the deny entry or starts with it as a component.
        if p == entry or p.startswith(entry + "/") or p.startswith(entry + os.sep):
            return True
    return False


# ---------------------------------------------------------------------------
# Bash command analysis
# ---------------------------------------------------------------------------

# Tokenise a shell command into argv-style tokens (simple, no full shell parse)
def _tokens(command: str) -> list:
    """Split a command string into whitespace-separated tokens, stripping quotes."""
    return re.split(r'\s+', command.strip())


def _is_grep_like(cmd: str) -> bool:
    """Does the command start with grep/rg?"""
    toks = _tokens(cmd)
    return bool(toks) and toks[0] in ("grep", "rg", "egrep", "fgrep")


def _grep_has_recursive_flag(cmd: str) -> bool:
    """Check whether a grep/rg invocation uses recursive search flags."""
    toks = _tokens(cmd)
    if not toks:
        return False
    # rg is always recursive by default
    if toks[0] == "rg":
        return True
    # grep: look for -r/-R or combined short flags containing r/R
    for tok in toks[1:]:
        if tok.startswith("-") and not tok.startswith("--"):
            # short flags like -r, -R, -rn, -Rl …
            flags = tok.lstrip("-")
            if "r" in flags or "R" in flags:
                return True
        elif tok in ("--recursive",):
            return True
    return False


def _grep_path_args(cmd: str) -> list:
    """
    Return the non-flag, non-pattern arguments to grep/rg that look like
    path arguments.  This is heuristic: we skip the command name, skip
    flags (tokens starting with -), and skip the first non-flag argument
    (which is the pattern for grep; rg also puts pattern first unless --).
    Returns the remaining tokens as candidate paths.
    """
    toks = _tokens(cmd)
    if not toks:
        return []

    tool = toks[0]
    rest = toks[1:]

    # Consume flags and their option-arguments; collect non-flag tokens.
    non_flags = []
    i = 0
    # flags that genuinely consume a following argument value
    FLAGS_CONSUMING_NEXT = {"-e", "--regexp", "-f", "--file",
                             "-m", "--max-count",
                             "-A", "--after-context",
                             "-B", "--before-context",
                             "-C", "--context",
                             "--color", "--colour",
                             "--include", "--exclude",
                             "--exclude-dir"}
    skip_next = False
    for tok in rest:
        if skip_next:
            skip_next = False
            continue
        if tok == "--":
            # everything after -- is paths
            idx = rest.index("--")
            non_flags.extend(rest[idx + 1:])
            break
        if tok.startswith("-"):
            if tok in FLAGS_CONSUMING_NEXT:
                skip_next = True
            elif "=" not in tok:
                # check combined form without = (e.g. -e pattern → consume next)
                base = tok.split("=")[0]
                if base in FLAGS_CONSUMING_NEXT:
                    skip_next = True
            continue
        non_flags.append(tok)

    # For grep: first non-flag is the pattern; the rest are paths.
    # For rg:   same convention.
    if len(non_flags) <= 1:
        return []
    return non_flags[1:]  # paths


def _is_unscoped_grep(cmd: str) -> bool:
    """
    Return True if this grep/rg call is recursive AND has no explicit,
    non-root path argument (i.e. no path, or path is '.' or '/').
    """
    if not _grep_has_recursive_flag(cmd):
        return False
    paths = _grep_path_args(cmd)
    if not paths:
        return True  # no path → searches cwd (repo root) recursively
    # If all paths are . or / → unscoped
    unscoped_roots = {".", "/", "./"}
    return all(p in unscoped_roots for p in paths)


def _find_is_unscoped(cmd: str) -> bool:
    """
    Return True if the command is a 'find' that starts at / or . without a
    -maxdepth or -path filter and without a prune action.
    """
    toks = _tokens(cmd)
    if not toks or toks[0] != "find":
        return False

    # find's first positional argument (after the command name) is the start path.
    # Flags before the path are rare; we handle the simple cases.
    start_path = None
    for tok in toks[1:]:
        if not tok.startswith("-"):
            start_path = tok
            break

    if start_path not in (".", "/", "./", None):
        # Explicit non-root path → scoped, allow
        return False

    cmd_lower = cmd.lower()
    # Check whether any depth/path-limiting options exist
    if "-maxdepth" in cmd_lower or "-path" in cmd_lower or "prune" in cmd_lower:
        return False

    return True


def _ls_r_over_deny(cmd: str, deny_paths: list) -> bool:
    """Return True if 'ls -R <deny_path>' or 'ls -lR <deny_path>' etc."""
    toks = _tokens(cmd)
    if not toks or toks[0] != "ls":
        return False
    # Check for -R flag
    has_r = any(
        (tok.startswith("-") and not tok.startswith("--") and "R" in tok.lstrip("-"))
        for tok in toks[1:]
    )
    if not has_r:
        return False
    # Any non-flag token that is a deny path?
    for tok in toks[1:]:
        if not tok.startswith("-") and _under_deny(tok, deny_paths):
            return True
    return False


def _cat_over_deny(cmd: str, deny_paths: list) -> bool:
    """Return True if 'cat <deny_path_file>'."""
    toks = _tokens(cmd)
    if not toks or toks[0] != "cat":
        return False
    for tok in toks[1:]:
        if not tok.startswith("-") and _under_deny(tok, deny_paths):
            return True
    return False


def _grep_over_deny(cmd: str, deny_paths: list) -> bool:
    """Return True if grep/rg explicitly targets a deny path."""
    if not _is_grep_like(cmd):
        return False
    if not _grep_has_recursive_flag(cmd):
        return False
    paths = _grep_path_args(cmd)
    return bool(paths) and any(_under_deny(p, deny_paths) for p in paths)


# ---------------------------------------------------------------------------
# Grep / Glob tool analysis
# ---------------------------------------------------------------------------

def _is_broad_pattern(pattern: str) -> bool:
    """Return True if the glob/grep pattern is very broad (**/* or *)."""
    p = pattern.strip()
    return p in ("**/*", "*", "**", "./**/*", "./*")


def _grep_glob_is_unscoped(tool_input: dict, deny_paths: list) -> bool:
    """
    For Grep/Glob tools:
    - path missing or is repo root AND pattern is very broad
    - OR path under a deny entry
    """
    path = tool_input.get("path", "") or ""
    pattern = tool_input.get("pattern", "") or tool_input.get("glob", "") or ""

    # Deny path check
    if path and _under_deny(path, deny_paths):
        return True

    # Broad pattern at root
    root_paths = {"", ".", "/", "./"}
    if path in root_paths and _is_broad_pattern(pattern):
        return True

    return False


# ---------------------------------------------------------------------------
# Read tool analysis
# ---------------------------------------------------------------------------

def _read_is_denied(tool_input: dict, deny_paths: list) -> bool:
    """Return True if file_path is under a deny path entry."""
    fp = tool_input.get("file_path", "") or ""
    if not fp:
        return False
    return _under_deny(fp, deny_paths)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        # Malformed input → fail-open
        _allow()

    try:
        cfg = store.get_config()
        if not cfg.get("scan_guard_enabled", True):
            _allow()

        deny_paths: list = cfg.get("scan_deny_paths", [])
        tool_name: str = data.get("tool_name", "")
        tool_input: dict = data.get("tool_input", {}) or {}

        # ------------------------------------------------------------------
        # Bash
        # ------------------------------------------------------------------
        if tool_name == "Bash":
            command: str = tool_input.get("command", "") or ""

            # 1. Unscoped recursive grep/rg
            if _is_grep_like(command) and _is_unscoped_grep(command):
                _deny("unscoped recursive grep/rg searches the entire repo")

            # 2. grep/rg targeting a deny path
            if _grep_over_deny(command, deny_paths):
                _deny("recursive search targets a noisy directory")

            # 3. Unscoped find
            if _find_is_unscoped(command):
                _deny("find without -maxdepth/-path/-prune scans the entire tree")

            # 4. ls -R over deny path
            if _ls_r_over_deny(command, deny_paths):
                _deny("recursive ls over a high-noise directory")

            # 5. cat over deny path
            if _cat_over_deny(command, deny_paths):
                _deny("cat over a high-noise directory")

            _allow()

        # ------------------------------------------------------------------
        # Grep / Glob
        # ------------------------------------------------------------------
        elif tool_name in ("Grep", "Glob"):
            tool_input_path = tool_input.get("path", "") or ""

            # Deny path check
            if tool_input_path and _under_deny(tool_input_path, deny_paths):
                _deny(f"search path is inside a high-noise directory ({tool_input_path})")

            # Broad unscoped pattern
            if _grep_glob_is_unscoped(tool_input, deny_paths):
                _deny("broad pattern at repo root would scan the entire tree")

            _allow()

        # ------------------------------------------------------------------
        # Read
        # ------------------------------------------------------------------
        elif tool_name == "Read":
            if _read_is_denied(tool_input, deny_paths):
                fp = tool_input.get("file_path", "")
                _deny(f"file path is inside a high-noise directory ({fp})")
            _allow()

        else:
            # Unknown tool → allow
            _allow()

    except Exception:
        # Any unexpected error → fail-open
        _allow()


if __name__ == "__main__":
    main()
