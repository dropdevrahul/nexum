# Plan: pi.dev CLI harness port

**Session:** _nosession
**Generated:** 2026-07-04
**Task summary:** Port all nexum features (hooks + commands + planner/executor) to pi.dev CLI harness via single TypeScript extension wrapping existing Python scripts.

**Models:**
- mechanical: any free model (e.g. nvidia/nemotron)
- standard: current session model (inherit)
- needs-strong: current session model (inherit)

---

### Step 1: Extension scaffolding + runScript helper
- route: mechanical
- files: `.pi/extensions/nexum-pi.ts`, `.pi/extensions/nexum-pi/package.json`
- objective: Create extension skeleton. Export default factory function. Resolve NEXUM_ROOT from extension path. Implement runScript() subprocess wrapper (spawn python3, pipe JSON stdin/stdout, fail-open). Track currentSessionId.
- contract: `.pi/extensions/nexum-pi/package.json` — empty package.json for jiti resolution. `.pi/extensions/nexum-pi.ts` — exports `export default function(pi: ExtensionAPI) { ... }`. Closes over `runScript(name, input, timeout?) → Promise<Record<string,any>>`, `NEXUM_ROOT` (abs path resolved from import.meta.url), `currentSessionId` (string, crypto.randomUUID() default). runScript sets `CLAUDE_PLUGIN_ROOT=NEXUM_ROOT` in subprocess env.
- scope: do NOT touch scripts/ dir, commands/ dir, agents/ dir, .opencode/ dir, hooks/ dir
- acceptance: `python3 -c "
import re
with open('.pi/extensions/nexum-pi.ts') as f: c = f.read()
assert 'export default function' in c
assert 'runScript' in c
assert 'NEXUM_ROOT' in c
assert 'currentSessionId' in c
print('PASS')
"`

### Step 2: Session lifecycle + tool interceptor events
- route: standard
- files: `.pi/extensions/nexum-pi.ts`
- objective: Wire pi.dev events to nexum Python scripts. session_start → session_reset.py + resume_nudge.py + audit_nudge.py. session_before_compact → precompact.py. tool_call → scan_guard.py (block/limit via permissionDecision, updatedInput) + predup.py (block re-read via permissionDecision). tool_result → dedup.py (collapse duplicate output, modify result content). Handle pi.dev event shapes -> Python script contracts.
- contract: Inside factory function body, after step 1 hooks. `pi.on("session_start", handler)`: reads event.reason/event.previousSessionFile, calls session_reset/resume_nudge/audit_nudge. `pi.on("session_before_compact", handler)`: calls precompact.py. `pi.on("tool_call", handler)`: uses isToolCallEventType to narrow, mutates event.input for scan_guard updatedInput, returns {block} for denied calls. `pi.on("tool_result", handler)`: calls dedup.py, returns {content/details} patches. All handlers fail-open (try/catch, silent return on error).
- scope: do NOT touch scripts/ dir, commands/ dir, .opencode/ dir, hooks/ dir
- acceptance: `python3 -c "
with open('.pi/extensions/nexum-pi.ts') as f: c = f.read()
assert \"pi.on('session_start'\" in c or 'pi.on(\"session_start\"' in c
assert \"pi.on('session_before_compact'\" in c or c.count('session_before_compact') > 0
assert \"pi.on('tool_call'\" in c or 'pi.on(\"tool_call\"' in c
assert \"pi.on('tool_result'\" in c or 'pi.on(\"tool_result\"' in c
print('PASS')
"`

### Step 3: nx-plan command
- route: needs-strong
- files: `.pi/extensions/nexum-pi.ts`
- objective: Register /nx-plan command. Handler: reads repo state (git branch, uncommitted changes, project structure via pi.exec or Bash), asks clarifying questions via ctx.ui.select/confirm/input, writes plan file to .nexum-data/plan/<session_id>.md, prompts model selection per tier, records choices in plan models section.
- contract: `pi.registerCommand("nx-plan", { description: "...", handler: async (args, ctx) => { ... } })`. Plan file follows format: `# Plan: <title>\n**Session:** <sid>\n**Models:**\n- mechanical: <model>\n...\n---\n### Step N: <title>\n- route: ...\n- files: ...\n- objective: ...\n- contract: ...\n- scope: ...\n- acceptance: ...`. Plan path = `<data_dir>/plan/<currentSessionId>.md`. Uses runScript("store.py", args) for config/session ops. Uses ctx.ui for all user interaction (select/confirm/input/notify).
- scope: do NOT touch scripts/ dir, commands/ dir, .opencode/ dir, hooks/ dir
- acceptance: `python3 -c "
with open('.pi/extensions/nexum-pi.ts') as f: c = f.read()
assert 'nx-plan' in c
assert 'pi.registerCommand' in c
print('PASS')
"`

### Step 4: nx-build command (orchestrator)
- route: needs-strong
- files: `.pi/extensions/nexum-pi.ts`
- objective: Register /nx-build command. Handler: reads plan file, parses steps + model selections, dispatches per tier group. For each group: pi.setModel(tierModel) → pi.sendUserMessage(step instructions) → ctx.waitForIdle() → run guardrail.py verification → record pass/fail. Retry same tier on fail (up to 1), escalate one tier (mechanical→standard→needs-strong). Final cost report via runScript("cost_report.py"). All step messages include command, objective, contract, scope, acceptance verbatim.
- contract: `pi.registerCommand("nx-build", { description: "...", handler: async (args, ctx) => { ... } })`. Plan read from `<data_dir>/plan/<currentSessionId>.md`. Parses plan models section. Step dispatch: for each step group (same route/tier), call `await pi.setModel(modelId); await pi.sendUserMessage(stepMsg); await ctx.waitForIdle();`. Acceptance check: `await pi.exec('python3 <NEXUM_ROOT>/scripts/guardrail.py --acceptance \"<cmd>\" --scope-root <root> --changed <files>')`. Retry: same tier up to 1 retry, escalate one tier if still failing. Cost summary: `await runScript("cost_report.py", { session_id })`. Print minimal output: failures, escalations, final cost. No per-step success chatter.
- scope: do NOT touch scripts/ dir, commands/ dir, .opencode/ dir, hooks/ dir
- acceptance: `python3 -c "
with open('.pi/extensions/nexum-pi.ts') as f: c = f.read()
assert 'nx-build' in c
assert 'setModel' in c or 'sendUserMessage' in c
print('PASS')
"`

### Step 5: Terminal commands — nx-save, nx-load, nx-audit, nx-report, nx-status
- route: mechanical
- files: `.pi/extensions/nexum-pi.ts`
- objective: Register 5 terminal commands. Each wraps a Python script call. nx-save: write session handoff to .nexum-data/handoff/<session_id>.md. nx-load: read latest handoff, display summary. nx-audit: run audit.py, show findings, offer --write fix. nx-report: run report.py, display session digest. nx-status: run report.py with --session flag, display compact stats.
- contract: `pi.registerCommand("nx-save", handler)` — resolves data dir, calls handoff.py via runScript, writes to .nexum-data/handoff/. `pi.registerCommand("nx-load", handler)` — reads .nexum-data/handoff/latest.md, shows summary. `pi.registerCommand("nx-audit", handler)` — runs audit.py, displays findings, ctx.ui.confirm for --write. `pi.registerCommand("nx-report", handler)` — runs report.py --session, displays output. `pi.registerCommand("nx-status", handler)` — runs report.py --session, displays compact stats. All use `ctx.ui.notify` or `ctx.ui.setWidget` for TUI display.
- scope: do NOT touch scripts/ dir, commands/ dir, .opencode/ dir, hooks/ dir
- acceptance: `python3 -c "
with open('.pi/extensions/nexum-pi.ts') as f: c = f.read()
for cmd in ['nx-save','nx-load','nx-audit','nx-report','nx-status']:
    assert cmd in c, f'{cmd} not found'
print('PASS')
"`

### Step 6: Integration verification
- route: standard
- files: `.pi/extensions/nexum-pi.ts`, `.pi/extensions/nexum-pi/package.json`
- objective: Verify extension file is valid. All events + commands registered. Python scripts callable from extension with CLAUDE_PLUGIN_ROOT set. Enumerate every feature and confirm.
- contract: Assertions: (a) file exports default function, (b) runScript exists, (c) NEXUM_ROOT resolved, (d) session_start handler registered, (e) session_before_compact handler registered, (f) tool_call handler with isToolCallEventType, (g) tool_result handler, (h) 7 commands registered (nx-plan, nx-build, nx-save, nx-load, nx-audit, nx-report, nx-status). Verify Python scripts accept stdin JSON: `echo '{}' | python3 scripts/session_reset.py` exits 0.
- scope: do NOT touch scripts/ dir, commands/ dir, .opencode/ dir, hooks/ dir
- acceptance: `python3 -c "
import re
with open('.pi/extensions/nexum-pi.ts') as f: c = f.read()
checks = [
    ('default function', 'export default function' in c),
    ('runScript', 'runScript' in c),
    ('session_start', 'session_start' in c),
    ('session_before_compact', 'session_before_compact' in c),
    ('tool_call', \"tool_call\" in c or 'tool_call' in c),
    ('tool_result', \"tool_result\" in c or 'tool_result' in c),
    ('nx-plan', 'nx-plan' in c),
    ('nx-build', 'nx-build' in c),
    ('nx-save', 'nx-save' in c),
    ('nx-load', 'nx-load' in c),
    ('nx-audit', 'nx-audit' in c),
    ('nx-report', 'nx-report' in c),
    ('nx-status', 'nx-status' in c),
    ('spawn import', 'spawn' in c),
]
fails = [name for name, ok in checks if not ok]
if fails:
    print(f'FAIL: {fails}')
    exit(1)
print('PASS')
"`
