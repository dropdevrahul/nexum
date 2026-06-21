# Configuration

nexum reads an optional JSON file at `<data_dir>/config.json`. Any key you set there is merged over the defaults — you only need to specify keys you want to change.

## Data directory

`<data_dir>` resolves in priority order:

1. `$CLAUDE_PLUGIN_DATA` if that environment variable is set
2. `${CLAUDE_PLUGIN_ROOT}/.nexum-data` if `CLAUDE_PLUGIN_ROOT` is set (it is, when nexum is loaded as a plugin)
3. `./.nexum-data` in the current working directory otherwise

The SQLite state file lives at `<data_dir>/nexum.db`.

## Configuration keys

| Key | Default | Description |
|-----|---------|-------------|
| `read_guard_enabled` | `true` | Enable the read-guard hook that injects a line limit for large files. |
| `read_guard_min_bytes` | `262144` | Files larger than this (in bytes) trigger the line-limit injection. |
| `read_guard_inject_lines` | `2000` | The `limit` value injected into the Read tool input. |
| `predup_enabled` | `true` | Enable pre-emptive dedup: deny identical repeated Read/Grep/Glob calls in the same session. |
| `predup_decision` | `"deny"` | Action when a duplicate is detected: `"deny"` silently blocks, `"ask"` prompts. |
| `predup_bash_readonly` | `false` | Whether to also cover read-only Bash commands (`cat`, `grep`, `ls`, `git log/diff/show/status/branch`). |
| `predup_max_age_seconds` | `3600` | How long a predup record is considered live. Records older than this are ignored. |
| `statusline_compaction_warn_pct` | `80` | Context-usage percentage at which the status line appends a `/compact` warning. Set to `0` to disable. |
| `statusline_compaction_warn_tokens` | `80000` | Absolute token count at which the status line appends a `/compact` warning, regardless of window percentage. Set to `0` to disable. |
| `plan_preview_enabled` | `true` | Show the projected cost preview before `/nx-build` dispatches any steps. |
| `resume_nudge_enabled` | `true` | Emit a session-start hint when a recent handoff exists for the current branch. |
| `resume_nudge_max_age_hours` | `24` | Maximum age (in hours) of a handoff for the resume nudge to fire. |
| `audit_nudge_enabled` | `true` | Surface an audit recommendation when context-blowing patterns are detected. |
| `route_calib_enabled` | `false` | Enable per-repo route calibration (nudges routes up when a tier's first-try pass rate is low). |
| `route_calib_min_samples` | `5` | Minimum number of dispatches before calibration nudges a route. |
| `route_calib_min_success_ratio` | `0.6` | First-try pass rate below which calibration nudges the route up one tier. |
| `max_steps_per_dispatch` | `6` | Maximum number of steps sent to a single executor dispatch (count cap; `0` disables). |
| `max_dispatch_context_tokens` | `50000` | Token budget per dispatch sub-batch (size cap used by `plan_preview.py`). |
| `dispatch_granularity` | `"group"` | `"group"`: send a whole route group to one executor; `"step"`: one dispatch per step. |
| `scan_guard_enabled` | `true` | Enable scan-guard blocking of context-blowing searches. |
| `scan_deny_paths` | `["node_modules", ".git", "dist", "build", "target", "vendor", ".next", "coverage", ".venv", "__pycache__"]` | Directory names that scan-guard and predup treat as deny-listed. |
| `handoff_auto_write_enabled` | `true` | Automatically write a handoff skeleton each prompt when context exceeds `handoff_threshold_tokens`. |
| `handoff_threshold_tokens` | `100000` | Token count at which the context-watch hook suggests `/nx-save` and begins writing auto-skeletons. |
| `compaction_threshold_tokens` | `120000` | Token count at which the context-watch hook suggests `/compact`. |
| `truncate_max_lines` | `200` | Maximum lines kept by the truncation hook. |
| `truncate_head_lines` | `120` | Lines kept from the head of a truncated output. |
| `truncate_tail_lines` | `60` | Lines kept from the tail of a truncated output. |
| `truncate_min_lines_to_act` | `240` | Minimum line count before truncation acts. |
| `orchestrator_resume_enabled` | `true` | Persist step verdicts to the step ledger so `/nx-build` can resume a partially-completed plan. |
| `max_same_tier_retries` | `1` | Number of same-tier retry attempts before escalating to the next tier. |

## Example config.json

```json
{
  "read_guard_min_bytes": 131072,
  "predup_bash_readonly": true,
  "statusline_compaction_warn_tokens": 60000,
  "plan_preview_enabled": true,
  "resume_nudge_max_age_hours": 48
}
```

To inspect the effective configuration at any time:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/store.py config
```
