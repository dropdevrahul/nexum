# Handoff: nexum live-hook + status-line investigation

> **RESOLVED 2026-06-17 — see "## RESOLUTION" at the bottom.** The shrink hook is inert
> because **Claude Code 2.1.178 does not honor the PostToolUse `updatedToolOutput` field**.
> Also: the central premise below ("live plugin = the cache") is **wrong** — live hooks run
> from the **worktree** (directory-type marketplace). The sections below are the original
> (partly-mistaken) notes; read the RESOLUTION first.

_Resume point for a clean session. Context: we set out to reduce context bloat when
`/nexum-implement` dispatches to subagents, then discovered the live plugin's core
shrink hook may be inert._

## Environment facts (load-bearing)
- **Live plugin = the installed cache, NOT the working tree.** Active install:
  `~/.claude/plugins/cache/nexum/nexum/0.2.1/` (commit `9ceb54c`). Cached versions
  present: 0.1.1, 0.2.0, 0.2.1. Working tree `/Users/rahultyagi/work/nexum` is on
  branch `review-fixes` (commit `84b85f4`) and its changes do NOT affect the live
  session until the plugin is reinstalled/updated.
- `claude --version` = **2.1.178**.
- statusLine in `~/.claude/settings.json` runs:
  `python3 "$(ls -dt ~/.claude/plugins/cache/nexum/nexum/*/scripts/statusline.py | head -1)"`
  → resolves to the 0.2.1 copy.

## Confirmed
1. **PreToolUse `scan_guard` fires in BOTH main agent and subagents.** Unscoped
   `find .` is blocked with the `[nexum]` message in a spawned subagent and in the
   main session.
2. **PostToolUse shrink (truncate/dedup) does NOT visibly act in the live session.**
   `seq 1 600` (600 lines > 240 truncate threshold, > 30 dedup gate) returned in full,
   no `[nexum] omitted N lines` marker — in main agent AND subagent.
3. **The scripts themselves are correct.** Feeding `dedup.py` a realistic Bash payload
   (`tool_response` as string and as `{stdout}`) directly produces a valid
   `{"hookSpecificOutput":{"hookEventName":"PostToolUse","updatedToolOutput":"…"}}`
   with the marker. Logic is fine.
4. **`updatedToolOutput` is a real, supported PostToolUse field**, nested inside
   `hookSpecificOutput` exactly as the script emits (confirmed via
   https://code.claude.com/docs/en/hooks). No min-version stated in docs.
5. **The live DB has no fresh activity.** `~/.claude/plugins/.../0.2.1/.nexum-data/nexum.db`
   `outputs` rows are all ~55h old; `savings` table empty. My `seq 1 600` added NO row
   → the PostToolUse hook most likely **did not fire (or errored before record_output)**
   in this session, even though PreToolUse did.

## Top open question
**Does the PostToolUse hook fire at all in the live session, or fire-but-`updatedToolOutput`-ignored?**
Decisive test for the clean session:
- Add an unmistakable side-effect to a COPY of the live hook (e.g. append a line to
  `/tmp/nexum-posttool-probe.log` at the very top of `dedup.py`/`truncate.py` main()),
  point a throwaway PostToolUse hook at it (or temporarily edit the cache copy), then run
  `seq 1 600` and check: (a) does the log line appear? → fires. (b) is output shrunk? → honored.
- Also verify the hook's **data dir**: it writes to `$CLAUDE_PLUGIN_DATA` or
  `$CLAUDE_PLUGIN_ROOT/.nexum-data`. Confirm which dir the LIVE hook actually uses (env
  may differ from our shell, where both were empty) — fresh rows may be landing elsewhere.
- If it fires but isn't honored: build a 5-line minimal PostToolUse hook that
  unconditionally replaces output, register it alone, and test whether 2.1.178 honors
  `updatedToolOutput` for Bash/Read at all.

## Status line "garbage" (user-reported, UNREPRODUCED)
- The resolved 0.2.1 `statusline.py` and the full `/bin/sh -c "$(ls -dt …)"` wrapper both
  print clean output locally (`nexum Opus 4.8  ·  ▓▓░░░░░░░░ 25%  ·  16.7k tok  ·  $0.42`)
  and fail-open to `nexum` on bad input. Could not reproduce the garbage.
- **Most likely cause:** the `▓` (U+2593) / `░` (U+2591) block-bar glyphs rendering as
  mojibake in the user's terminal/font. **Need the user to paste/describe one render**
  (boxes? traceback? escape codes? raw JSON?) — the fix differs by cause.
- Likely fix if encoding: swap the bar glyphs for ASCII (e.g. `#`/`-`) in `statusline.py`.
  Fix in repo, then reinstall — or edit the cache copy for immediate relief.

## Secondary bug found
- **scan_guard tokenizer doesn't handle quoted args.** `grep -r "def " .` was NOT blocked
  because `_tokens` splits on whitespace, so the quoted pattern's space injects a bogus
  path token and `_is_unscoped_grep` sees a non-`.` "path". Real-world quoted patterns with
  spaces evade the guard. (`tests/test_scan_guard.py` only uses space-free patterns.)

## Note on the original goal
The brainstormed bloat improvements (minimal subagent returns, bound batch size,
shrink-based fail logs, orchestrator-as-subagent) are **moot until the shrink hook is
confirmed working live** — fix the foundation first.

## Already done this session (committed, branch `review-fixes`, `84b85f4`)
Review-finding fixes + task-handling overhaul + metering. See commit body. Untracked scratch:
`nexum-review.md` (the original review), this file.

---

## RESOLUTION (2026-06-17, clean session)

### Headline
**Claude Code 2.1.178 (the latest published version — `npm view @anthropic-ai/claude-code
version` == 2.1.178) does NOT honor the PostToolUse `updatedToolOutput` field.** The hook
fires and emits perfectly-formed JSON; CC just doesn't apply it. nexum's entire
truncate/dedup context-savings feature is therefore **inert on current Claude Code**,
independent of script correctness.

### How it was proven (decisive, reproducible)
Instrumented the *worktree* `dedup.py` (see below for why worktree) to log its emit decision,
plus a throwaway probe in the separate `campy` PostToolUse hook:

1. **nexum emits a real shrink, ignored.** A 600-line unique output produced:
   `[dedup-WT] EMIT updatedToolOutput in_len=10691 out_len=3165 acted=True`
   — yet the model received the full 10691-char output, no `[nexum] omitted` marker.
2. **A minimal foreign hook is also ignored.** Made `campy/.../post-tool-use.sh` emit
   `{"updatedToolOutput":"__MARKER__"}` (tested BOTH nested-in-`hookSpecificOutput` and
   top-level). Output never replaced.
3. **Single-emitter, ruling out multi-hook clobber.** Triggered a `Write` (nexum's matcher
   `Read|Bash|Grep|Glob` does NOT match Write, so only campy's `*` hook ran). Still ignored.
   → not a hook-ordering/merge artifact; the field is simply unsupported in 2.1.178.

Docs (`code.claude.com/docs/en/hooks`) DO list `updatedToolOutput` as the PostToolUse
output-replacement field — so this is a docs-vs-behavior gap (likely an unshipped/broken
feature), not a usage error on our side.

### The handoff's two mistakes (corrected)
- **Live hooks run from the WORKTREE, not the cache.** nexum's marketplace `source` is
  `directory: /Users/rahultyagi/work/nexum` (see `~/.claude/plugins/known_marketplaces.json`),
  so `CLAUDE_PLUGIN_ROOT` resolves to the worktree. Probes added to the worktree scripts fired;
  probes in the 0.2.1 cache copy never did. All prior cache-focused evidence (cache
  `.nexum-data` DB staleness, instrumenting the cache) examined the wrong files. Fresh DB rows
  land in the **worktree's** `.nexum-data`.
- **The PostToolUse hook DOES fire** every matching call (`dedup.py` fired reliably). The
  handoff's "didn't fire" conclusion came from checking the cache copy + cache DB. It fires;
  it just can't mutate output.

### Other facts nailed down
- Worktree `hooks/hooks.json` registers ONLY `dedup.py` for PostToolUse (not `truncate.py`);
  dedup re-applies `truncate.shrink()` internally. So "truncate didn't fire" is by design.
- Real PostToolUse payload for **Bash**: `tool_response` = dict with keys
  `[stdout, stderr, interrupted, isImage, noOutputExpected]`. `extract_output` handles this
  (reads `stdout`). Top-level keys: `session_id, transcript_path, cwd, permission_mode, effort,
  hook_event_name, tool_name, tool_input, tool_response, tool_use_id, duration_ms`.
- **Read-tool bug:** for `Read`, `tool_response` = `{type, file}` (content nested under `file`),
  so `extract_output` returns None → nexum can never shrink Read output, even if the field
  worked. Fix `extract_output` to dig into `file` when/if the feature becomes usable.
- PreToolUse control (scan_guard's permission decision) is a SEPARATE mechanism and is the only
  currently-working savings lever; only PostToolUse output-replacement is broken. (Note: a bare
  `find .` was NOT blocked this session — scan_guard's match logic may need a recheck, but that's
  orthogonal to the shrink finding.)

### Implication / next move (the real decision)
The shrink-via-PostToolUse approach cannot work on current CC. Options, in order of sanity:
1. **Verify upstream**: is `updatedToolOutput` actually implemented in 2.1.178? File an issue /
   watch the changelog. If it's coming, gate nexum's claims behind a version check + fail loud.
2. **Pivot the savings lever** to something that works today: PreToolUse input shaping (e.g.
   auto-appending output limiters), or scan_guard-style blocking of known-noisy commands.
3. Until then, **stop advertising truncate/dedup savings** — they don't materialize live.

The original subagent-bloat improvements remain moot until a working savings mechanism exists.

### Cleanup done
All probes removed and reverted: worktree `scripts/{dedup,truncate}.py` (`git checkout`),
cache 0.2.1 copies (manual revert, parse-checked), `campy/.../post-tool-use.sh` (restored from
backup). Scratch files (`/tmp/nexum-posttool-probe.log`, test flag, backup) deleted. `git status`
clean for both repos' touched files.

---

## UPSTREAM CORROBORATION (2026-06-17) — it's a known, unfixed CC bug

Searched `anthropics/claude-code` issues. My finding is independently confirmed by multiple
reporters using the *exact same* additionalContext-vs-updatedToolOutput discriminator:

- **#67442** (closed dup, v2.1.173): "`updatedToolOutput` silently ignored for built-in tools
  (Bash, WebFetch)". Ruled out wrong-field, not-firing (additionalContext + permissionDecision
  work), and multi-hook — same as us.
- **#65403** (closed dup): "still not honored for Bash — secrets leak". States plainly:
  **"it has never worked for the Bash tool."**
- **#54196** (v2.1.121): same, auto-closed for inactivity (NOT_PLANNED).
- **#32105** (closed COMPLETED 2026-04-24): the feature request that is *literally nexum's use
  case* ("context budget recovery", Bash output compression via PostToolUse). Marked shipped in
  v2.1.121 per changelog — but the bug reports above prove it does **not** actually work for
  built-in tools. Also documents the key distinction: **`updatedMCPToolOutput` works but ONLY for
  MCP tools**; `updatedToolOutput` for built-in tools is the broken path.
- Issues just bounce through an auto-duplicate chain (#65122→#64326→…) with no maintainer ETA.
  A third-party external executor (`ironrun`) exists as a workaround for the redaction case.

**Net:** `updatedToolOutput` for built-in Bash/Read/WebFetch has been broken across every version
from 2.1.121 → 2.1.178 (current latest), for months, with no fix in sight. nexum's PostToolUse
shrink cannot work on any shippable CC today. This is NOT a near-term version-gate-and-wait.

### Working levers that DO exist (corroborated by #32105's own alternatives table)
- **PreToolUse `updatedInput`** — WORKS. For **Read**, inject `limit`/`offset` when a file is
  large → genuinely caps Read output. (The one clean, supported win.)
- **PreToolUse command-shaping for Bash** — rewrite/limit known-verbose commands. Real but
  whack-a-mole, and blind to output size. **Do NOT** auto-return `permissionDecision:"allow"` to
  force rewrites (RTK anti-pattern — bypasses the permission/safety system; see #32105).
- **scan_guard PreToolUse blocking** — already works (permission decisions are honored).
- **`additionalContext`** — works, but ADDS tokens; useless for shrinking.
- **MCP tool outputs** — `updatedMCPToolOutput`/`updatedToolOutput` works for MCP, not built-ins.

### Recommended nexum changes
1. **Honesty fix (do first):** stop reporting truncate/dedup "savings" for Bash/Read — they don't
   materialize. Either remove the claim or gate it behind a runtime self-test (emit a probe
   `updatedToolOutput` once per session; only count savings if it actually took effect).
2. **Pivot the real lever to PreToolUse `updatedInput` for Read** (large-file limit/offset).
3. **Keep scan_guard** as the blocking lever; optionally add conservative Bash command-shaping.
4. **Auto-reactivation:** ship the session self-test so the PostToolUse shrink turns itself back
   on automatically if/when CC fixes built-in `updatedToolOutput`. Track #65403/#32105.
