# Releasing nexum

nexum ships as a self-hosted Claude Code plugin marketplace (this repo). The
version in `.claude-plugin/plugin.json` is the **source of truth**: Claude Code
ignores the marketplace entry's version whenever `plugin.json` declares one, and
users only receive an update when that string changes. Keep all three in sync —
`plugin.json`, the `marketplace.json` entry, and the git tag.

## Versioning

[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`):

- **PATCH** — bug fixes, no user-visible behavior change.
- **MINOR** — new commands/hooks/options, backward compatible.
- **MAJOR** — breaking changes to commands, hooks, config, or the data layout.

## Cutting a release

1. **Changelog** — move items from `## [Unreleased]` into a new
   `## [X.Y.Z] - YYYY-MM-DD` section in `CHANGELOG.md`.
2. **Bump** both manifests in lockstep:
   ```bash
   python tools/bump_version.py X.Y.Z
   ```
3. **Verify** locally:
   ```bash
   python tools/check_version.py
   python -m unittest discover -s tests
   ```
4. **Commit**:
   ```bash
   git commit -am "Release vX.Y.Z"
   ```
5. **Tag and push** — this triggers the Release workflow:
   ```bash
   git tag vX.Y.Z
   git push origin main --tags
   ```

## Automation

- **CI** (`.github/workflows/ci.yml`) runs on every push/PR to `main`: the test
  suite on Python 3.9 / 3.11 / 3.13, JSON manifest validation, and the
  version-consistency check.
- **Release** (`.github/workflows/release.yml`) runs on any `v*` tag: it runs the
  tests, asserts the tag matches the manifest versions (`check_version.py --tag`),
  and publishes a GitHub Release whose notes are the matching `CHANGELOG.md`
  section (`changelog.py`).

## After release

Users pull the new version from inside Claude Code:

```
/plugin marketplace update nexum
```
