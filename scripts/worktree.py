"""worktree.py — create an isolated git worktree for a divergent task.

When context_watch detects the user has switched to a genuinely different task
*while the working tree still has uncommitted changes*, nexum isolates the new
work in a git worktree under ``<repo>/.nexum-data/worktrees/<slug>`` so it does
not tangle with the unfinished changes. A clean tree needs no worktree — the
current context is fine for the new task.

Untracked helper files the new worktree needs but git won't check out (env
files, local config) are copied in per the ``worktree_copy`` config globs;
anything matching ``worktree_ignore`` is skipped.

Everything here is fail-open: any git/filesystem error returns None (or False)
rather than raising, so the calling hook never blocks a prompt on a git hiccup.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        timeout=8,
    )


def _toplevel(cwd: str) -> Optional[str]:
    try:
        r = _git(cwd, "rev-parse", "--show-toplevel")
    except Exception:
        return None
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def is_dirty(cwd: str) -> bool:
    """True if the working tree at *cwd* has uncommitted (tracked or untracked)
    changes. Fail-open to False so a git error never forces a worktree."""
    try:
        r = _git(cwd, "status", "--porcelain")
    except Exception:
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _copy_extras(
    src_root: str,
    dest_root: str,
    copy_globs: Iterable[str],
    ignore_globs: Iterable[str],
) -> None:
    """Copy files matching *copy_globs* (relative to src_root) into dest_root,
    preserving relative paths, skipping anything matching *ignore_globs*."""
    ignore = list(ignore_globs or [])
    for pattern in copy_globs or []:
        for path in Path(src_root).glob(pattern):
            rel = path.relative_to(src_root)
            rel_str = str(rel)
            if any(fnmatch.fnmatch(rel_str, ig) for ig in ignore):
                continue
            target = Path(dest_root) / rel
            try:
                if path.is_dir():
                    shutil.copytree(path, target, dirs_exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            except Exception:
                # Best-effort: one un-copyable extra must not abort the worktree.
                continue


def create_worktree(
    cwd: str,
    slug: str,
    copy_globs: Iterable[str] = (),
    ignore_globs: Iterable[str] = (),
) -> Optional[str]:
    """Create (or reuse) a worktree at ``<repo>/.nexum-data/worktrees/<slug>``.

    Returns the worktree path, or None if not a git repo / creation failed.
    A new branch ``nexum/<slug>`` is created off HEAD; if the path already
    exists it is reused as-is.
    """
    top = _toplevel(cwd)
    if not top:
        return None

    dest = Path(top) / ".nexum-data" / "worktrees" / slug
    if dest.exists():
        return str(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    branch = f"nexum/{slug}"
    try:
        r = _git(top, "worktree", "add", "-b", branch, str(dest))
        if r.returncode != 0:
            # Branch likely already exists — attach the worktree to it instead.
            r = _git(top, "worktree", "add", str(dest), branch)
            if r.returncode != 0:
                return None
    except Exception:
        return None

    _copy_extras(top, str(dest), copy_globs, ignore_globs)
    return str(dest)


def _demo() -> None:
    """Self-check: a dirty repo yields a worktree with copied extras; is_dirty
    tracks the tree state. Run: python3 scripts/worktree.py"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        _git(tmp, "init", "-q")
        _git(tmp, "config", "user.email", "t@t")
        _git(tmp, "config", "user.name", "t")
        (Path(tmp) / "a.txt").write_text("hello")
        _git(tmp, "add", "a.txt")
        _git(tmp, "commit", "-qm", "init")
        assert not is_dirty(tmp), "clean tree read as dirty"

        # untracked helper + a tracked edit → dirty
        (Path(tmp) / ".env").write_text("SECRET=1")
        (Path(tmp) / "a.txt").write_text("changed")
        assert is_dirty(tmp), "dirty tree read as clean"

        wt = create_worktree(tmp, "feature-abc123", copy_globs=[".env"], ignore_globs=[])
        assert wt is not None and os.path.isdir(wt), "worktree not created"
        assert (Path(wt) / ".env").read_text() == "SECRET=1", "extra not copied"
        assert (Path(wt) / "a.txt").read_text() == "hello", "worktree not at HEAD"
        # reuse returns same path
        assert create_worktree(tmp, "feature-abc123") == wt, "reuse mismatch"
    print("worktree demo OK")


def _main() -> None:
    """CLI: `--create --repo <r> --slug <s>` prints {"worktree": path}. With no
    args, runs the self-check demo."""
    import argparse
    import json as _json
    import sys

    if len(sys.argv) == 1:
        _demo()
        return

    p = argparse.ArgumentParser(prog="worktree.py")
    p.add_argument("--create", action="store_true")
    p.add_argument("--repo", required=True)
    p.add_argument("--slug", required=True)
    args = p.parse_args()

    try:
        cfg = {}
        try:
            import store  # optional — for worktree_copy/ignore config
            cfg = store.get_config()
        except Exception:
            pass
        wt = create_worktree(
            os.path.realpath(args.repo), args.slug,
            cfg.get("worktree_copy", []), cfg.get("worktree_ignore", []),
        )
        print(_json.dumps({"worktree": wt} if wt else {"error": "worktree creation failed"}))
    except Exception as exc:
        print(_json.dumps({"error": str(exc)}))


if __name__ == "__main__":
    _main()
