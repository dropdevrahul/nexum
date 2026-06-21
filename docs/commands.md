# Commands

nexum registers six slash commands inside Claude Code. Each is backed by a command definition in the `commands/` directory of the plugin.

## /nx-plan

Produce a step-by-step implementation plan for the current task, routing each step to the cheapest model that can execute it reliably.

Run `/nx-plan` with a description of your task. The planner (running on Opus) decomposes the work into self-contained steps, assigns each a route (`mechanical` → Haiku, `standard` → Sonnet, `needs-strong` → Opus), and writes the plan to `<data_dir>/plan/<session_id>.md`. Each step carries an explicit contract, file list, scope guard, and runnable acceptance test so a weaker model can execute it without additional context.

When the plan is written, the planner prints the path and a one-line summary of each step. Review the plan before running `/nx-build`.

## /nx-build

Execute a nexum plan file by dispatching steps to the cheapest capable model tier, verifying acceptance, and escalating on failure.

`/nx-build` reads the plan file for the current session, prints a projected cost preview (see below), then executes steps in tier order (mechanical → standard → needs-strong), batching where possible to reuse the warm model prefix. Each step is verified by running its acceptance command; a failing step is retried on the same tier and then escalated. Results are persisted to a step ledger so a restarted session resumes from where it left off rather than redoing completed steps.

### Cost preview

Before dispatching any steps, `/nx-build` prints a projected cost breakdown when `plan_preview_enabled` is true (the default):

```
[nexum] Plan cost preview (estimate)
  Steps: 9  |  Per-step heuristic: 8,000 in / 2,000 out tokens
  Note: token counts are a per-step heuristic, not measured usage.

  Tier           Steps    Input tok   Output tok   Actual $   Baseline $
  --------------------------------------------------------------------
  haiku              3       24,000        6,000   $0.0027      $0.0900
  sonnet             5       40,000       10,000   $0.0600      $0.1500
  opus               1        8,000        2,000   $0.0540      $0.0540
  --------------------------------------------------------------------
  TOTAL              9       72,000       18,000   $0.1167      $0.2940

Projected: $0.1167 vs all-opus $0.2940 — saves $0.1773 (60.3%)
```

The numbers are a per-step heuristic. Authoritative post-run totals — capturing prompt-cache writes/reads and actual token counts — come from the cost report printed after all steps complete.

Configure via `config.json`:

```json
{ "plan_preview_enabled": true }
```

## /nx-audit

Audit the current repo's Claude Code ignore configuration and flag noise files or directories that could blow context.

`/nx-audit` runs `scripts/audit.py` against the current working directory and summarizes findings: missing ignore files, unignored noise directories (`node_modules`, `.git`, `dist`, etc.), gaps between `.gitignore` and the Claude Code ignore file, and large or binary files that could consume context. After presenting findings it offers to apply recommended patterns with `--write` (idempotent — never duplicates patterns already present).

## /nx-status

Install the nexum session-usage status line into your Claude Code settings.

`/nx-status` presents the `statusLine` settings block and offers to merge it into your user-level (`~/.claude/settings.json`) or project-level (`.claude/settings.json`) settings. See [Status line](status-line.md) for details on what the status line displays and how to configure it.

## /nx-save

Write a session handoff so you can continue in a fresh session once a context or plan limit is near.

`/nx-save` gathers concrete facts about the current session — git branch and status, the stored task, decisions made, what has been verified — and writes a rich handoff to `<data_dir>/handoff/<session_id>.md` and `<data_dir>/handoff/latest.md`. A fresh session can then read `latest.md` directly, or run `/nx-load` to have nexum load and summarize it.

## /nx-load

Resume from the most recent session handoff written by `/nx-save` or the auto-handoff hook.

`/nx-load` reads `<data_dir>/handoff/latest.md`, checks its freshness and branch against the current git state, and summarizes the goal and next steps. It does not load a handoff automatically — the user must invoke it explicitly. If the actual git state contradicts the handoff notes, the discrepancy is surfaced rather than acted on silently.
