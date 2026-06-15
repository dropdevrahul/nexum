#!/usr/bin/env python3
"""Bump the plugin version in plugin.json and marketplace.json in lockstep.

Usage:
    python tools/bump_version.py <new-version>   # e.g. 0.2.0

Edits only the `"version"` fields, preserving file formatting. Does NOT touch
CHANGELOG.md or create git tags -- see RELEASING.md for the full flow.
Stdlib only.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"]
SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+].+)?$")
VERSION_FIELD = re.compile(r'("version":\s*")[^"]*(")')


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: bump_version.py <new-version>  e.g. 0.2.0")
    new = sys.argv[1].lstrip("v")
    if not SEMVER.match(new):
        sys.exit(f"ERROR: not a semantic version: {new!r}")

    for rel in FILES:
        p = ROOT / rel
        text = p.read_text()
        updated, n = VERSION_FIELD.subn(rf"\g<1>{new}\g<2>", text)
        if n == 0:
            sys.exit(f"ERROR: {rel}: no 'version' field found")
        p.write_text(updated)
        print(f"{rel}: version -> {new} ({n} occurrence(s))")

    print("\nNext: update CHANGELOG.md, commit, then tag vX.Y.Z (see RELEASING.md).")


if __name__ == "__main__":
    main()
