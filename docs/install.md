# Install

This repository is its own Claude Code plugin marketplace. Install nexum from within Claude Code:

```
/plugin marketplace add dropdevrahul/nexum
/plugin install nexum@nexum
```

`/plugin install` enables the plugin immediately and the hooks take effect on the next interaction.

## Local checkout

To try nexum from a local checkout instead of the published marketplace entry:

```
/plugin marketplace add ./path/to/nexum
/plugin install nexum@nexum
```

## Updating

To pull a new release after it is published:

```
/plugin marketplace update nexum
```

This fetches the latest version from the marketplace source you registered. After updating, any version-pinned paths (such as the status line command) are kept version-independent by design — see [Status line](status-line.md) for details.
