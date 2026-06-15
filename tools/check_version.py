#!/usr/bin/env python3
"""Verify plugin.json and marketplace.json declare the same version.

Optionally assert that version also matches a release tag (e.g. v0.1.0).
Stdlib only. Exits non-zero on any mismatch so CI/release can gate on it.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(rel):
    return json.loads((ROOT / rel).read_text())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", help="release tag to check against, e.g. v0.1.0")
    args = ap.parse_args()

    plugin = load(".claude-plugin/plugin.json")
    market = load(".claude-plugin/marketplace.json")

    name = plugin.get("name")
    pv = plugin.get("version")
    if not pv:
        sys.exit("ERROR: .claude-plugin/plugin.json is missing 'version'")

    errors = []
    entries = [e for e in market.get("plugins", []) if e.get("name") == name]
    if not entries:
        errors.append(f"marketplace.json has no plugin entry named {name!r}")
    for e in entries:
        if e.get("version") != pv:
            errors.append(
                f"version mismatch: plugin.json={pv} "
                f"marketplace entry {name!r}={e.get('version')}"
            )

    if args.tag:
        tag = args.tag[1:] if args.tag.startswith("v") else args.tag
        if tag != pv:
            errors.append(f"tag {args.tag} does not match plugin.json version {pv}")

    if errors:
        for e in errors:
            print("ERROR:", e, file=sys.stderr)
        sys.exit(1)

    suffix = f" and matches tag {args.tag}" if args.tag else ""
    print(f"OK: version {pv} is consistent across manifests{suffix}")


if __name__ == "__main__":
    main()
