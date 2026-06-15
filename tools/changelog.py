#!/usr/bin/env python3
"""Print the CHANGELOG.md section body for a given version.

Used by the release workflow to build GitHub release notes. Accepts either
"0.1.0" or "v0.1.0". Falls back to a one-line note if the section is absent.
Stdlib only.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def section(version):
    ver = version[1:] if version.startswith("v") else version
    lines = (ROOT / "CHANGELOG.md").read_text().splitlines()
    head = re.compile(r"^##\s+\[?" + re.escape(ver) + r"\]?")
    nexthead = re.compile(r"^##\s+")
    out, capturing = [], False
    for line in lines:
        if capturing:
            if nexthead.match(line):
                break
            out.append(line)
        elif head.match(line):
            capturing = True
    return "\n".join(out).strip()


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: changelog.py <version>")
    body = section(sys.argv[1]) or f"Release {sys.argv[1]}."
    print(body)


if __name__ == "__main__":
    main()
