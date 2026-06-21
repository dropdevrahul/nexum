# Contributing

## Running the tests

nexum uses only the Python standard library, so there are no dependencies to install. Run the test suite from the repo root:

```
python3 -m pytest tests/ -q
```

Or with the stdlib test runner (what CI uses):

```
python -m unittest discover -s tests -v
```

Both work. CI runs the unittest variant on Python 3.9, 3.11, and 3.13.

## Code rules

**Stdlib only.** Allowed imports are: `json`, `sqlite3`, `hashlib`, `os`, `sys`, `re`, `subprocess`, `pathlib`, `time`, `fnmatch`, `argparse`, `dataclasses`, `typing`. No third-party packages, no pip installs. This constraint is permanent — the plugin must work in a fresh Claude Code environment with no setup.

**Fail-open.** Every hook script wraps its logic in try/except. On any internal error it prints `{}` to stdout and exits 0. A bug in nexum must never crash or block a user's Claude Code session.

**Deterministic JSON.** Any JSON emitted by a hook uses `json.dumps(obj, sort_keys=True)`. Non-deterministic output invalidates the prompt cache and costs more than it saves.

**No timestamps in model-visible content.** Timestamps and UUIDs must not appear in any content that feeds a model prefix — they make every message unique and break caching.

## Adding a feature

1. Add or edit the relevant script under `scripts/`.
2. Add or update tests under `tests/test_<module>.py`.
3. Make sure `python -m unittest discover -s tests` passes.
4. If the feature is user-visible, add or update the relevant docs page.

## Releasing

nexum uses semantic versioning (`MAJOR.MINOR.PATCH`):

- **PATCH** — bug fixes, no user-visible behavior change.
- **MINOR** — new commands, hooks, or options, backward compatible.
- **MAJOR** — breaking changes to commands, hooks, config, or data layout.

To cut a release:

1. Move items from `## [Unreleased]` in `CHANGELOG.md` into a new `## [X.Y.Z] - YYYY-MM-DD` section.
2. Bump both manifests in lockstep: `python tools/bump_version.py X.Y.Z`
3. Verify locally: `python tools/check_version.py` and `python -m unittest discover -s tests`
4. Commit: `git commit -am "Release vX.Y.Z"`
5. Tag and push: `git tag vX.Y.Z && git push origin main --tags`

Pushing the tag triggers the Release workflow, which runs the test suite, verifies the tag matches the manifest versions, and publishes a GitHub Release with the matching changelog section as the release notes.

Users pull the new version from within Claude Code:

```
/plugin marketplace update nexum
```
