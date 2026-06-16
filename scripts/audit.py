"""
audit.py — Nexum ignore-file auditor.

CLI:
    python3 audit.py [--root <dir>] [--write]

Scans <root> (default: cwd) and reports:
  1. No ignore file at all.
  2. Noise dirs that exist on disk but are not matched by any ignore pattern.
  3. Entries in .gitignore not covered by the Claude ignore file (reconcile).
  4. Files > 5 MB or binary blobs likely to blow context if read.

With --write: appends a `# nexum` block of suggested patterns to the chosen
ignore file (idempotent — never duplicates, never deletes existing lines).

Stdlib only.  Imports store (scripts dir) for get_config (scan_deny_paths).
"""

import argparse
import fnmatch
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Bootstrap: make sure the scripts/ directory is on sys.path so that
# `import store` works when this file is run directly.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402  (stdlib-only rule applies to *this* project; store is ours)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Additional common noise dirs beyond scan_deny_paths (union used for finding #2).
_COMMON_NOISE_DIRS: List[str] = [
    ".DS_Store",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".eggs",
    "*.egg-info",
    "__pycache__",
    ".cache",
    "tmp",
    "temp",
    ".terraform",
    ".gradle",
    ".m2",
    "out",
    "bin",
    "obj",
]

# Files larger than this are flagged as context-blowers (bytes).
_SIZE_THRESHOLD = 5 * 1024 * 1024  # 5 MB

# Heuristic binary-detection: read this many bytes and look for null bytes.
_BINARY_SAMPLE = 8192

# The nexum block marker used in ignore files.
_NEXUM_MARKER = "# nexum"


# ---------------------------------------------------------------------------
# Ignore-file detection
# VERIFY the real Claude Code ignore filename at integration time.
# As of the v1 spec, `.claudeignore` is the primary candidate; `.gitignore`
# is the fallback.  Check the Claude Code docs/release notes to confirm the
# exact filename before shipping.
# ---------------------------------------------------------------------------

def ignore_files(root: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (chosen_ignore_path, label) for the ignore file to audit/write.

    Priority:
      1. .claudeignore  (Claude Code's native ignore, if present)
      2. .gitignore     (fallback)

    Returns (None, None) when neither file exists.

    NOTE: VERIFY the real Claude Code ignore filename at integration time.
    The spec targets .claudeignore first; if Claude Code actually uses a
    different name, change the candidates list below.
    """
    # VERIFY the real Claude Code ignore filename at integration time.
    candidates = [
        (".claudeignore", ".claudeignore (Claude Code native)"),
        (".gitignore",    ".gitignore (fallback — Claude Code may not use this directly)"),
    ]
    for filename, label in candidates:
        path = os.path.join(root, filename)
        if os.path.isfile(path):
            return path, label
    return None, None


# ---------------------------------------------------------------------------
# Pattern helpers (fnmatch-based)
# ---------------------------------------------------------------------------

def _read_patterns(path: str) -> List[str]:
    """Read non-empty, non-comment lines from an ignore file."""
    patterns: List[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.rstrip("\n").strip()
                if stripped and not stripped.startswith("#"):
                    patterns.append(stripped)
    except (OSError, PermissionError):
        pass
    return patterns


def _is_matched(name: str, patterns: List[str]) -> bool:
    """Return True if *name* (basename or relative path) matches any pattern.

    Supports simple fnmatch patterns as used in .gitignore / .claudeignore.
    A pattern ending with / is treated as a directory-only match (we strip the
    trailing slash and match the name).
    """
    for pat in patterns:
        # Strip trailing slash (dir-only marker in gitignore syntax)
        p = pat.rstrip("/")
        if not p:
            continue
        if fnmatch.fnmatch(name, p):
            return True
        # Also try matching just the basename against patterns that contain no
        # path separator (most common case).
        if "/" not in p and fnmatch.fnmatch(os.path.basename(name), p):
            return True
    return False


# ---------------------------------------------------------------------------
# Binary-file detection
# ---------------------------------------------------------------------------

def _is_binary(path: str) -> bool:
    """Return True if the file looks binary (contains null bytes in first 8 KB)."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(_BINARY_SAMPLE)
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Disk scan helpers
# ---------------------------------------------------------------------------

def _top_level_names(root: str) -> List[str]:
    """Return names (dirs + files) immediately under root, no recursion."""
    try:
        return os.listdir(root)
    except (OSError, PermissionError):
        return []


def _walk_for_large_files(
    root: str,
    deny_dirs: List[str],
    ignore_patterns: List[str],
) -> List[Tuple[str, int, bool]]:
    """Walk root and return (rel_path, size_bytes, is_binary) for large / binary files.

    Skips:
    - Symlinks into deny dirs (don't follow).
    - Dirs matching deny_dirs or ignore_patterns.
    - Permission-error paths (skip & continue).
    """
    results: List[Tuple[str, int, bool]] = []
    all_deny = set(deny_dirs) | {d.rstrip("/") for d in ignore_patterns}

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune dirs in-place to avoid descending into noise / denied dirs.
        pruned = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            # Skip symlinks to avoid following them into deny dirs.
            if os.path.islink(full):
                continue
            # Prune if matches deny or ignore patterns.
            if _is_matched(d, list(all_deny)) or d in deny_dirs:
                continue
            pruned.append(d)
        dirnames[:] = pruned

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if os.path.islink(fpath):
                continue
            try:
                size = os.path.getsize(fpath)
            except (OSError, PermissionError):
                continue

            if size > _SIZE_THRESHOLD:
                binary = _is_binary(fpath)
                rel = os.path.relpath(fpath, root)
                results.append((rel, size, binary))
            elif size > 0:
                binary = _is_binary(fpath)
                if binary:
                    rel = os.path.relpath(fpath, root)
                    results.append((rel, size, True))

    return results


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------

def run_audit(root: str) -> dict:
    """Perform the audit and return a structured findings dict.

    Keys:
      ignore_path: str | None
      ignore_label: str | None
      missing_ignore: bool
      unignored_noise_dirs: list[str]   -- dirs on disk not covered by patterns
      gitignore_not_in_claude: list[str]  -- .gitignore entries absent from claudeignore
      large_or_binary: list[tuple(rel_path, size, is_binary)]
    """
    cfg = store.get_config()
    scan_deny: List[str] = cfg.get("scan_deny_paths", [])
    all_noise = list(dict.fromkeys(scan_deny + _COMMON_NOISE_DIRS))  # deduplicated, order preserved

    ignore_path, ignore_label = ignore_files(root)
    missing_ignore = ignore_path is None

    ignore_patterns: List[str] = []
    if ignore_path:
        ignore_patterns = _read_patterns(ignore_path)

    # ------------------------------------------------------------------ #
    # Finding 2: noise dirs that EXIST but are not matched by any pattern  #
    # ------------------------------------------------------------------ #
    unignored_noise: List[str] = []
    for entry in _top_level_names(root):
        full = os.path.join(root, entry)
        if not os.path.isdir(full):
            continue
        if os.path.islink(full):
            continue
        # Is this name (or matches a pattern) in the noise set?
        if _is_matched(entry, all_noise) or entry in all_noise:
            # It's a noise dir — is it covered by the ignore file?
            if not _is_matched(entry, ignore_patterns):
                unignored_noise.append(entry)

    # ------------------------------------------------------------------ #
    # Finding 3: .gitignore entries absent from the Claude ignore file     #
    # (only meaningful when the chosen ignore is NOT .gitignore itself)   #
    # ------------------------------------------------------------------ #
    gitignore_not_covered: List[str] = []
    if ignore_path and not ignore_path.endswith(".gitignore"):
        gitignore_path = os.path.join(root, ".gitignore")
        if os.path.isfile(gitignore_path):
            git_patterns = _read_patterns(gitignore_path)
            for pat in git_patterns:
                if pat not in ignore_patterns and not _is_matched(pat, ignore_patterns):
                    gitignore_not_covered.append(pat)

    # ------------------------------------------------------------------ #
    # Finding 4: large (>5 MB) or binary files                             #
    # ------------------------------------------------------------------ #
    large_or_binary = _walk_for_large_files(root, scan_deny, ignore_patterns)

    return {
        "ignore_path": ignore_path,
        "ignore_label": ignore_label,
        "missing_ignore": missing_ignore,
        "unignored_noise_dirs": unignored_noise,
        "gitignore_not_in_claude": gitignore_not_covered,
        "large_or_binary": large_or_binary,
    }


# ---------------------------------------------------------------------------
# Suggested patterns builder
# ---------------------------------------------------------------------------

def _suggested_patterns(findings: dict) -> List[str]:
    """Build the list of patterns nexum wants to add to the ignore file."""
    patterns: List[str] = []
    # Add unignored noise dirs.
    for d in findings["unignored_noise_dirs"]:
        patterns.append(d + "/")
    # Add gitignore entries not yet in claudeignore (reconcile).
    for pat in findings["gitignore_not_in_claude"]:
        if pat not in patterns:
            patterns.append(pat)
    return patterns


# ---------------------------------------------------------------------------
# Idempotent --write logic
# ---------------------------------------------------------------------------

def _write_ignore(ignore_path: str, new_patterns: List[str]) -> Tuple[List[str], List[str]]:
    """Append a `# nexum` block to *ignore_path* with *new_patterns*.

    Idempotent:
    - If a `# nexum` block already exists, update it in place (add missing
      patterns, never duplicate, never delete existing lines).
    - Returns (added, skipped) pattern lists.
    """
    # Read existing content.
    try:
        with open(ignore_path, "r", encoding="utf-8", errors="replace") as fh:
            original_lines = fh.readlines()
    except (OSError, PermissionError):
        original_lines = []

    # Collect ALL patterns already in the file (for dedup check).
    existing_patterns = _read_patterns(ignore_path)

    # Determine which new_patterns are genuinely absent.
    to_add: List[str] = []
    skipped: List[str] = []
    for pat in new_patterns:
        bare = pat.rstrip("/")
        already = any(
            p.rstrip("/") == bare or p == pat
            for p in existing_patterns
        )
        if already:
            skipped.append(pat)
        else:
            to_add.append(pat)

    if not to_add:
        return [], skipped

    # Check whether a `# nexum` block already exists.
    nexum_block_start: Optional[int] = None
    nexum_block_end: Optional[int] = None
    in_nexum = False
    for i, line in enumerate(original_lines):
        stripped = line.rstrip("\n").strip()
        if stripped == _NEXUM_MARKER:
            nexum_block_start = i
            in_nexum = True
            continue
        if in_nexum:
            # The block ends at the next blank line or a new comment section
            # that is NOT a nexum-added pattern or sub-comment.
            if stripped.startswith("#") and not stripped.startswith("# nexum"):
                nexum_block_end = i
                in_nexum = False
            elif stripped == "":
                nexum_block_end = i
                in_nexum = False

    if in_nexum:
        nexum_block_end = len(original_lines)

    if nexum_block_start is not None:
        # Update in place: insert new patterns just before the block end.
        insert_at = nexum_block_end if nexum_block_end is not None else len(original_lines)
        new_lines = [p + "\n" for p in to_add]
        updated = original_lines[:insert_at] + new_lines + original_lines[insert_at:]
    else:
        # Append a fresh block at the end.
        # Ensure there's a trailing newline before our block.
        separator = ""
        if original_lines and not original_lines[-1].endswith("\n"):
            separator = "\n"
        block_lines = [separator + _NEXUM_MARKER + "\n"] + [p + "\n" for p in to_add]
        updated = original_lines + block_lines

    try:
        with open(ignore_path, "w", encoding="utf-8") as fh:
            fh.writelines(updated)
    except (OSError, PermissionError) as exc:
        print(f"[nexum] ERROR: could not write {ignore_path}: {exc}", file=sys.stderr)
        return [], skipped

    return to_add, skipped


def _create_ignore_with_block(path: str, patterns: List[str]) -> None:
    """Create a new ignore file containing only the nexum block."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_NEXUM_MARKER + "\n")
            for pat in patterns:
                fh.write(pat + "\n")
        print(f"[nexum] Created {path} with {len(patterns)} pattern(s).")
    except (OSError, PermissionError) as exc:
        print(f"[nexum] ERROR: could not create {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Human report
# ---------------------------------------------------------------------------

def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def print_report(findings: dict, root: str) -> None:
    """Print a human-readable audit report to stdout."""
    print(f"[nexum] Audit root: {root}")
    print()

    # Ignore file status
    if findings["missing_ignore"]:
        print("[nexum] Finding 1 — NO ignore file found (.claudeignore or .gitignore).")
        print("        Recommend creating .claudeignore to prevent Claude from reading noise.")
    else:
        print(f"[nexum] Ignore file: {findings['ignore_label']}")
        print(f"        Path: {findings['ignore_path']}")

    print()

    # Unignored noise dirs
    noise = findings["unignored_noise_dirs"]
    if noise:
        print(f"[nexum] Finding 2 — {len(noise)} noise dir(s) exist on disk but are NOT ignored:")
        for d in noise:
            print(f"        {d}/")
    else:
        print("[nexum] Finding 2 — All known noise dirs are either absent or already ignored.")

    print()

    # gitignore vs claudeignore reconcile
    gap = findings["gitignore_not_in_claude"]
    if gap:
        print(f"[nexum] Finding 3 — {len(gap)} .gitignore pattern(s) not present in .claudeignore:")
        for pat in gap:
            print(f"        {pat}")
    elif not findings["missing_ignore"] and not findings["ignore_path"].endswith(".gitignore"):
        print("[nexum] Finding 3 — .claudeignore covers all .gitignore patterns (or no .gitignore).")
    else:
        print("[nexum] Finding 3 — (skipped: using .gitignore as the Claude ignore file)")

    print()

    # Large / binary files
    lob = findings["large_or_binary"]
    if lob:
        print(f"[nexum] Finding 4 — {len(lob)} large or binary file(s) that may blow context:")
        for rel, size, binary in lob:
            kind = "binary" if binary else f"{_fmt_size(size)}"
            print(f"        {rel}  ({kind})")
    else:
        print("[nexum] Finding 4 — No large (>5 MB) or binary files found outside ignored dirs.")

    print()

    # Summary
    any_finding = (
        findings["missing_ignore"]
        or noise
        or gap
        or lob
    )
    if not any_finding:
        print("[nexum] Result: clean — no issues found.")
    else:
        print("[nexum] Result: issues found (see above). Run with --write to apply suggestions.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="audit.py",
        description="[nexum] Audit ignore-file coverage and context-blowing files.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Directory to audit (default: current working directory).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Append suggested patterns to the ignore file (idempotent).",
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root) if args.root else os.getcwd()

    if not os.path.isdir(root):
        print(f"[nexum] ERROR: --root {root!r} is not a directory.", file=sys.stderr)
        sys.exit(1)

    findings = run_audit(root)
    print_report(findings, root)

    if args.write:
        print()
        suggested = _suggested_patterns(findings)

        if not suggested:
            print("[nexum] --write: no new patterns to add.")
            return

        ignore_path = findings["ignore_path"]

        if ignore_path is None:
            # No ignore file exists — create .claudeignore (preferred).
            # VERIFY the real Claude Code ignore filename at integration time.
            ignore_path = os.path.join(root, ".claudeignore")
            _create_ignore_with_block(ignore_path, suggested)
            print(f"[nexum] --write: created {ignore_path} with {len(suggested)} pattern(s).")
        else:
            added, skipped = _write_ignore(ignore_path, suggested)
            if added:
                print(f"[nexum] --write: added {len(added)} pattern(s) to {ignore_path}:")
                for p in added:
                    print(f"        + {p}")
            if skipped:
                print(f"[nexum] --write: {len(skipped)} pattern(s) already present (skipped):")
                for p in skipped:
                    print(f"        ~ {p}")
            if not added and not skipped:
                print(f"[nexum] --write: nothing to do — {ignore_path} is up to date.")


if __name__ == "__main__":
    main()
