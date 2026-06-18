# Nexum Codebase Review

## What It Does

**Nexum** is a Claude Code plugin that reduces context token usage and model costs via three pillars:

1. **Context-savings hooks** — `truncate.py` shrinks large tool outputs (head+tail+error-lines), `dedup.py` collapses repeated identical outputs into one-line pointers
2. **Cost-driven planner & executor** — `nexum-plan`/`nexum-implement` commands route work to Haiku/Sonnet/Opus subagents based on complexity tier, with guardrails and retry/escalation
3. **Lifecycle & hygiene guards** — `context_watch.py` detects mid-session task switches (e.g. fix→feature) and blocks them; `scan_guard.py` denies unscoped recursive searches; `audit.py` finds unignored noisy dirs

---

## Logic Flaws

### Behavioral Issues

1. **`truncate.py:96-100` — dead code branch**
   The inner `if len(text) > 10000` is guaranteed true (outer check on line 94 already passed it); the `else: return (text, False)` branch is unreachable.

2. **`truncate.py:83` vs SPEC — error-line cap mismatch**
   SPEC says "cap extra at 40 lines" but the config default is `truncate_max_lines: 200` and the code uses that value. SPEC and implementation disagree by 5x.

3. **`context_watch.py:229` — blocked prompts inflate token total**
   `_accumulate_tokens()` runs before any guard check, so every blocked prompt counts toward the compaction warning threshold. A user repeatedly hitting the intent guard can trigger a false compaction warning.

4. **`context_watch.py:114-116` — fragile task-type prefix matching**
   `_derive_task_type` returns the first match in dict-iteration order, making results dependent on insertion ordering. A word that matches both "integrat" (feature) and "debug" (fix) arbitrarily picks whichever dict entry comes first.

5. **`audit.py:123-141` — `_is_matched` ignores `!` negation patterns**
   Gitignore supports `!important.log` to un-ignore a file, but the audit treats it as ignored, producing false negatives.

### Redundant / Dead Code

6. **`scan_guard.py:79-82` — duplicate path check**
   The second `raw` block (`path.lstrip("/")`) is entirely redundant with the first `p` block. Every case it could catch is already caught by the first normalization.

7. **`audit.py:360` — redundant condition**
   `stripped != _NEXUM_MARKER` is subsumed by `not stripped.startswith("# nexum")`.

### Design Concerns

8. **`store.py:196-223` — in-memory fallback is a mirage**
   If the SQLite file corrupts, each call to `db()` creates a *fresh* in-memory DB with no shared state. All helpers that call `db()+close()` (like `seen_output` then `record_output`) see different databases. The fallback silently turns all hooks into no-ops.

9. **`store.py:191` — `PRAGMA foreign_keys=ON` on a schema with zero foreign keys**
   Harmless but misleading.

10. **`cost_report.py:75` — "all-opus baseline" inflates savings**
    The report compares actual cost to what everything *would* cost at Opus rates. Since the alternative to routing isn't "all Opus", this savings figure is synthetic. A comparison against "all Sonnet" would be more honest.

11. **`scan_guard.py:190-201` — `_is_unscoped_grep` false-positives on `grep -r --include='*.py'`**
    A grep with `--include`/`--exclude-dir` but no explicit path is treated as unscoped, even though it's semantically scoped by file type.

12. **`dedup.py:127-135` — savings recorded even when dedup was a no-op on the model**
    If the prior output was already truncated down to ~200 lines and the repeat is collapsed to a pointer, the "saved" count compares original(1000 lines) - pointer(1 line) = 999, but the model only ever saw ~200 lines from the first occurrence. The savings figure overstates what was *actually* avoided in context.
