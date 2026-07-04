# Nexum → OpenCode Port

**Date:** 2026-07-04
**Status:** Design

Port the nexum Claude Code plugin to OpenCode. Nexum is a context-token and model-cost optimization plugin. The port keeps the existing Python scripts as-is and wires them into OpenCode's plugin, command, and agent system.

---

## 1. Architecture

The Python scripts in `scripts/` are the engine — they stay entirely unchanged. The port is a **wiring layer**:

```
┌─────────────────────────────────────────────┐
│  OpenCode (.opencode/)                       │
│                                              │
│  plugins/nexum-hooks.ts  ← event →          │──── calls ──→  scripts/*.py
│  commands/nx-*.md        ← /nx-*             │              (via subprocess)
│  agents/nexum-*.md       ← @nexum-*          │
│  opencode.json           ← config            │
└─────────────────────────────────────────────┘
```

Every Python script reads JSON from stdin and writes JSON to stdout (the same contract they already follow). The TS plugin transforms OpenCode event objects to/from the nexum format.

**Env var set by plugin (via `shell.env`):**

| Var | Value | Used by |
|---|---|---|
| `NEXUM_ROOT` | Absolute path to nexum repo root | Commands/agents resolve `scripts/` |

The plugin sets `NEXUM_ROOT` at startup so commands and agents can find the Python scripts without hardcoding paths. Session ID is passed per-call by the plugin (it has access to `sessionID` from the plugin context).

`NEXUM_ROOT` is set by resolving `import.meta.dirname` (Bun) or `__dirname` minus the `.opencode/plugins/` suffix — the plugin always knows where it lives relative to the repo root.

---

## 2. Plugin — event wiring (`.opencode/plugins/nexum-hooks.ts`)

Single TypeScript file that wires Python scripts into OpenCode events. Each handler transforms the OpenCode event shape into nexum's stdin JSON format, calls the script, and applies the result.

### Events mapped

| OpenCode Event | Nexum Script | What it does |
|---|---|---|
| `tool.execute.before` | `scan_guard.py` | Block/limit unscoped reads, recursive searches |
| `tool.execute.before` | `predup.py` | Block re-read of already-loaded file content |
| `tool.execute.after` | `dedup.py` | Collapse repeated tool output + truncate oversized |
| `session.created` | `resume_nudge.py` | Nudge user about unfinished handoff |
| `session.created` | `audit_nudge.py` | Periodic ignore-file audit reminder |
| `session.created` | `session_reset.py` | Clear stale tool_calls, throttle retention prune |
| `session.compacted` | `precompact.py` | Clear tool_calls at compaction boundary |

### Missing Claude Code hooks (no OpenCode equivalent)

| Claude Hook | Script | Resolution |
|---|---|---|
| `UserPromptSubmit` | `context_watch.py` | **Skipped.** Intent-guard and auto-handoff-write are non-critical. The auto-handoff can run on `session.compacted` instead. |
| `SubagentStop` | `subagent_usage.py` | **Skipped.** OpenCode tracks subagent usage natively. |

### Plugin contract

```typescript
// NexumScript Input (from plugin handler, matching what each script expects):
// scan_guard:  { tool_name: string, tool_input: object }
// predup:      { tool_name: string, tool_input: object, session_id: string }
// dedup:       { tool_name: string, tool_response: string|object, session_id: string }
// resume_nudge/audit_nudge/session_reset: { session_id: string }
// precompact:  { session_id: string }

// NexumScript Output (parsed from script stdout):
// scan_guard:  { hookSpecificOutput: { permissionDecision?, permissionDecisionReason?, updatedInput? } }
// predup:      { hookSpecificOutput: { permissionDecision?, permissionDecisionReason? } }
// dedup:       { hookSpecificOutput: { updatedToolOutput? } }
// others:      {} (fail-open)
```

The plugin handler:
1. Serializes the OpenCode event into the script's expected input shape
2. Spawns `python3 <NEXUM_ROOT>/scripts/<script>.py`, pipes JSON via stdin
3. Parses stdout JSON
4. Applies any `permissionDecision`, `updatedInput`, `updatedToolOutput` back to the OpenCode output object
5. On any error (parse failure, non-zero exit, timeout) → no-op (fail-open)

---

## 3. Commands (`.opencode/commands/`)

7 markdown command files, one per `/nx-*` command.

| Command | Frontmatter Model | Source |
|---|---|---|
| `nx-plan.md` | Plan agent | Adapted from `commands/nx-plan.md` |
| `nx-build.md` | Build agent | Adapted from `commands/nx-build.md` |
| `nx-save.md` | Build agent | Adapted from `commands/nx-save.md` |
| `nx-load.md` | Build agent | Adapted from `commands/nx-load.md` |
| `nx-audit.md` | Haiku-tier model | Adapted from `commands/nx-audit.md` |
| `nx-report.md` | Haiku-tier model | Adapted from `commands/nx-report.md` |
| `nx-status.md` | (no frontmatter needed) | New: displays nexum session stats via a Python script call |

### Changes from Claude Code versions

1. **Path resolution.** `${CLAUDE_PLUGIN_ROOT}` → `$NEXUM_ROOT` (set by plugin via `shell.env`).
2. **Session ID.** `$CLAUDE_SESSION_ID` → `$NEXUM_SESSION_ID` (set by plugin).
3. **Model frontmatter.** Claude Code uses `model: haiku/sonnet/opus` — OpenCode uses `model: provider/model-id` or omits to inherit.
4. **Agent references.** Claude Code `@nexum-impl-haiku` → OpenCode `@nexum-mechanical`.

### nx-plan: model selection at finalization

`/nx-plan` gains a new final step — after the agent drafts the tiered plan, it asks the user to pick models for each tier:

> Plan drafted. Which model for mechanical steps? (default: anthropic/claude-haiku-4-20250514)
> Which model for standard steps? (default: anthropic/claude-sonnet-4-20250514)
> Which model for needs-strong steps? (default: anthropic/claude-opus-4-20250514)

The choices are recorded in the plan file under a `models:` section:

```markdown
**Models:**
- mechanical: anthropic/claude-haiku-4-20250514
- standard: anthropic/claude-sonnet-4-20250514
- needs-strong: anthropic/claude-opus-4-20250514
```

### nx-build: dispatch to chosen models

`/nx-build` reads the `models:` section from the plan and dispatches each step to the appropriate model. The 3 executor agents are defined **without** a `model` in frontmatter (so they inherit the caller's model). Dispatch approach:

1. **Tier groups** are dispatched sequentially via the Task tool to the named subagent (`@nexum-mechanical`, etc.)
2. **The model choice from the plan is passed as an instruction** in the dispatch prompt: "You are the mechanical executor. The user selected {model} for this tier. Use it."
3. The subagent inherits the session's primary model, but the prompt instructs it which model to target. The user must have the chosen models configured in their OpenCode setup.
4. **Batch dispatch** for efficiency: all steps sharing a tier go to one subagent invocation, reusing the shared spec/context.

This is not perfect model enforcement (the subagent inherits the primary session model), but it's the cleanest v1 approach with OpenCode's current agent dispatch. The model choice is documented in the plan for the user to configure accordingly.

### nx-status: TUI display

`/nx-status` runs the nexum report script and displays:
- Token savings
- Session cost
- Waste analysis

Since OpenCode has no persistent statusLine, this is an **on-demand** command. For periodic nudges, the plugin can use `tui.toast.show` on `session.status` events.

---

## 4. Agents (`.opencode/agents/`)

4 subagent markdown files.

| File | Purpose | Model |
|---|---|---|
| `nexum-mechanical.md` | Executor for mechanical steps (boilerplate, well-specified CRUD) | Inherited from plan |
| `nexum-standard.md` | Executor for standard steps (multi-file, some reasoning) | Inherited from plan |
| `nexum-needs-strong.md` | Executor for complex steps (architecture, ambiguity, debugging) | Inherited from plan |
| `nexum-reviewer.md` | Code reviewer for escalated/high-risk steps | Fixed (user's strongest model) |

### Changes from Claude Code versions

Config format change (`model: haiku` → `model:` inherited or explicit). The executors reference `python3 $NEXUM_ROOT/scripts/guardrail.py` instead of `${CLAUDE_PLUGIN_ROOT}`.

### Executor agent prompt (all three are near-identical, only model differs)

```
You are a nexum executor. Implement the given step(s) in order.
Implement ONLY the listed step(s). Do not touch files outside scope.
After each step, verify via:
  python3 $NEXUM_ROOT/scripts/guardrail.py \
    --acceptance "<cmd>" --scope-root <root> --changed <files>
Return: on PASS → step index + one-line summary + guardrail JSON.
        on FAIL → same + unified diff.
```

---

## 5. Configuration (`opencode.json`)

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "command": {
    "nx-plan": { "agent": "plan", "description": "Decompose task into tiered, self-contained steps" },
    "nx-build": { "description": "Execute a nexum plan via cheapest capable tiers" },
    "nx-save": { "description": "Write session handoff for clean resume" },
    "nx-load": { "description": "Resume from most recent session handoff" },
    "nx-audit": { "description": "Audit repo for context-risk files" },
    "nx-report": { "description": "Show session digest: cost + wasted-context analysis" },
    "nx-status": { "description": "Show nexum session stats" }
  },
  "agent": {
    "nexum-mechanical": {
      "mode": "subagent",
      "description": "Executor for mechanical/boilerplate nexum steps",
      "permission": { "edit": "allow", "bash": "allow" }
    },
    "nexum-standard": {
      "mode": "subagent",
      "description": "Executor for standard nexum steps",
      "permission": { "edit": "allow", "bash": "allow" }
    },
    "nexum-needs-strong": {
      "mode": "subagent",
      "description": "Executor for complex nexum steps",
      "permission": { "edit": "allow", "bash": "allow" }
    },
    "nexum-reviewer": {
      "mode": "subagent",
      "description": "Reviews escalated nexum step implementations",
      "permission": { "edit": "deny", "bash": "allow" }
    }
  }
}
```

---

## 6. Pricing simplification

The Claude Code version has a hardcoded `PRICING` dict in `store.py` with Opus/Sonnet/Haiku rates. OpenCode supports arbitrary models, so:

- **Default:** Token-only tracking (no dollar conversion). Report shows raw token savings.
- **Optional:** User can set `$/1M-tok` rates per model via nexum config (`config.json`) if they want dollar amounts.
- The pricing keys in `store.py` change from fixed model names to config-driven entries.

---

## 7. File changes summary

### New files (OpenCode layer)
```
.opencode/
  plugins/nexum-hooks.ts       ~120 lines — event wiring
  commands/nx-plan.md          adapted from commands/nx-plan.md
  commands/nx-build.md         adapted from commands/nx-build.md
  commands/nx-save.md          adapted from commands/nx-save.md
  commands/nx-load.md          adapted from commands/nx-load.md
  commands/nx-audit.md         adapted from commands/nx-audit.md
  commands/nx-report.md        adapted from commands/nx-report.md
  commands/nx-status.md        new — on-demand stats display
  agents/nexum-mechanical.md   adapted from agents/nexum-impl-haiku.md
  agents/nexum-standard.md     adapted from agents/nexum-impl-sonnet.md
  agents/nexum-needs-strong.md adapted from agents/nexum-impl-opus.md
  agents/nexum-reviewer.md     adapted from agents/nexum-reviewer.md
opencode.json                  ~50 lines — config
```

### Existing files changed
```
scripts/store.py               minor — pricing made configurable
```

`context_watch.py` and `subagent_usage.py` need no changes — they simply won't be triggered (no hook calls them), which is fine (fail-open). Only `store.py`'s pricing table needs adjustment for model-agnostic tracking.

---

## 8. Build order

1. `opencode.json` — config (no deps)
2. `.opencode/plugins/nexum-hooks.ts` — event wiring (calls existing scripts, test standalone)
3. `.opencode/commands/` — all 7 commands (adapt from existing, test each via `/nx-*`)
4. `.opencode/agents/` — all 4 agents (adapt from existing, test via `/nx-plan` → `/nx-build`)
5. `scripts/store.py` pricing change — make configurable
6. Integration test: `/nx-plan` → model selection → `/nx-build` → acceptance checks
