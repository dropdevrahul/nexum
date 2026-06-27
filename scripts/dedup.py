#!/usr/bin/env python3
"""
dedup.py — Nexum deduplication hook (PostToolUse).

Runs AFTER truncate.py in the hook chain but reads the ORIGINAL tool output
from its own stdin (the two are separate processes and each receive the
unmodified hook input). Per §1 EDGE CASE: dedup is the AUTHORITY on the
final updatedToolOutput — it re-applies truncate.shrink() so the emitted
text is both deduplicated and truncated in one pass.

Contract:
- Only acts on outputs >= 30 lines OR >= 2000 chars.  Tiny outputs → emit {}.
- Computes h = store.sha256(output).
  * If store.seen_output(session_id, h) exists → emit pointer, omit body.
  * Else: shrunk,_ = truncate.shrink(output, cfg); record; emit shrunk.
- Fail-open: any unhandled error → print {} exit 0.
- Deterministic JSON: json.dumps(..., sort_keys=True).
"""

import json
import os
import sys


# ---------------------------------------------------------------------------
# Bootstrap: make sure scripts/ dir is on sys.path so `import store` and
# `import truncate` work when invoked as python3 scripts/dedup.py from any cwd.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store      # noqa: E402  (must come after path setup)
import truncate   # noqa: E402


# ---------------------------------------------------------------------------
# Thresholds for "worth deduplicating"
# ---------------------------------------------------------------------------
_MIN_LINES = 30
_MIN_CHARS = 2000

# Edit-family tools tracked for wasted-context analytics (file_activity).
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


def _track_file_activity(session_id, tool_name, tool_input, data):
    """Record per-file read/edit counters for the /nx-report waste analytics.

    A Read accumulates the file's read count + injected-token estimate (and
    flags a partial read when offset/limit is set); an edit marks the file
    useful. Fail-open and independent of the dedup logic below.
    """
    if tool_name == "Read":
        fp = tool_input.get("file_path")
        if not fp:
            return
        out = truncate.extract_output(data) or ""
        toks = store.estimate_tokens(out) if out else 0
        partial = bool(tool_input.get("offset") or tool_input.get("limit"))
        store.record_file_read(session_id, fp, toks, partial)
    elif tool_name in _EDIT_TOOLS:
        fp = tool_input.get("file_path")
        if fp:
            store.record_file_edit(session_id, fp)

# Self-test: PostToolUse `updatedToolOutput` is silently ignored for built-in
# tools (Bash/Read/Grep/Glob) on current Claude Code (anthropics/claude-code
# #65403, #67442, #32105). When it is ignored, the shrunk/pointer output we emit
# never reaches the model, so recording a "saving" for it would be fiction. We
# therefore gate savings on a per-session, transcript-verified self-test:
#   uto_works flag: "yes" → replacements take effect, record real savings.
#                   "no"  → ignored, record nothing (honest).
#                   unset → undetermined; emit a probe and confirm via transcript.
# This also auto-reactivates savings if a future Claude Code honors the field.
_UTO_FLAG = "uto_works"
_UTO_PROBE = "uto_probe"
# Minimum char gap between original and emitted output before a shrink is usable
# as a self-test probe (so honored-vs-ignored is unambiguous by length).
_PROBE_MIN_GAP = 200


def _verify_pending_probe(session_id: str, transcript_path: str) -> None:
    """Resolve a pending updatedToolOutput self-test probe against the transcript.

    If the model's recorded tool result matches the length we emitted → the
    replacement was honored (flag "yes", and back-record the probe's saving).
    If it matches the original length → ignored (flag "no"). Undetermined
    (transcript not yet flushed) → leave pending and retry on a later call.
    """
    if store.get_flag(session_id, _UTO_FLAG):
        return  # already determined
    probe_raw = store.get_flag(session_id, _UTO_PROBE)
    if not probe_raw:
        return
    try:
        probe = json.loads(probe_raw)
    except Exception:
        store.set_flag(session_id, _UTO_PROBE, "")
        return
    actual = store.transcript_tool_result_len(transcript_path, probe.get("tool_use_id", ""))
    if actual is None:
        return  # not yet written; try again next invocation
    emitted = int(probe.get("emitted_len", 0))
    original = int(probe.get("original_len", 0))
    if abs(actual - emitted) <= abs(actual - original):
        # Replacement landed — the field is honored this session.
        store.set_flag(session_id, _UTO_FLAG, "yes")
        store.record_saving(
            session_id,
            probe.get("source", "truncate"),
            int(probe.get("saved_tok", 0)),
            probe.get("effective_tok"),
        )
    else:
        store.set_flag(session_id, _UTO_FLAG, "no")
    store.set_flag(session_id, _UTO_PROBE, "")


def _maybe_record_saving(
    session_id: str,
    source: str,
    saved: int,
    effective,
    tool_use_id: str,
    emitted_len: int,
    original_len: int,
) -> None:
    """Record a saving only if the session self-test confirms replacements work.

    Before the test resolves, stash one probe describing this emission so a later
    call can verify it via the transcript. Never over-counts: an unverified or
    failed self-test records nothing.
    """
    if saved <= 0:
        return
    verdict = store.get_flag(session_id, _UTO_FLAG)
    if verdict == "yes":
        store.record_saving(session_id, source, saved, effective)
        return
    if verdict == "no":
        return  # replacement ignored by the harness — no real saving
    # Undetermined: arm a single probe (needs an unambiguous length gap + an id).
    if (
        tool_use_id
        and (original_len - emitted_len) >= _PROBE_MIN_GAP
        and not store.get_flag(session_id, _UTO_PROBE)
    ):
        store.set_flag(
            session_id,
            _UTO_PROBE,
            json.dumps(
                {
                    "tool_use_id": tool_use_id,
                    "emitted_len": emitted_len,
                    "original_len": original_len,
                    "saved_tok": saved,
                    "effective_tok": effective,
                    "source": source,
                }
            ),
        )


def _is_large(text: str) -> bool:
    """Return True if the output is large enough to be worth deduplicating."""
    if len(text) >= _MIN_CHARS:
        return True
    if text.count("\n") + 1 >= _MIN_LINES:
        return True
    return False


def _make_summary(output: str, token_count: int) -> str:
    """Build a short summary: first non-empty line + token estimate."""
    first_line = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped[:120]  # cap at 120 chars
            break
    return f"{first_line} (~{token_count} tokens)"


def main() -> None:
    """PostToolUse hook entry point."""
    try:
        # ----------------------------------------------------------------
        # 1. Parse stdin JSON
        # ----------------------------------------------------------------
        try:
            data = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            print("{}")
            return

        if not isinstance(data, dict):
            print("{}")
            return

        # ----------------------------------------------------------------
        # 1b. File-activity tracking (wasted-context analytics) — runs for
        #     reads AND edits, independent of the dedup size logic. Edits never
        #     need dedup, so record and return immediately for them.
        # ----------------------------------------------------------------
        _fa_tool = data.get("tool_name") or "unknown"
        _fa_session = data.get("session_id") or "_nosession"
        _fa_input = data.get("tool_input") or {}
        try:
            if store.get_config().get("file_activity_enabled", True):
                _track_file_activity(_fa_session, _fa_tool, _fa_input, data)
        except Exception:
            pass
        if _fa_tool in _EDIT_TOOLS:
            print("{}")
            return

        # ----------------------------------------------------------------
        # 2. Extract tool output
        # ----------------------------------------------------------------
        output = truncate.extract_output(data)
        if not output:
            # Empty or missing output — nothing to do.
            print("{}")
            return

        # ----------------------------------------------------------------
        # 3. Size gate: only dedup large outputs
        # ----------------------------------------------------------------
        if not _is_large(output):
            print("{}")
            return

        # ----------------------------------------------------------------
        # 4. Session / tool metadata
        # ----------------------------------------------------------------
        session_id = data.get("session_id") or "_nosession"
        tool_name = data.get("tool_name") or "unknown"
        tool_use_id = data.get("tool_use_id") or ""
        transcript_path = data.get("transcript_path") or ""

        # Resolve any outstanding self-test probe from a prior call: did the
        # output we replaced last time actually reach the model? (See the
        # _UTO_* helpers above.) Cheap no-op once the verdict is known.
        try:
            _verify_pending_probe(session_id, transcript_path)
        except Exception:
            pass

        # ----------------------------------------------------------------
        # 5. Load config (needed for shrink; fail-open if unavailable)
        # ----------------------------------------------------------------
        try:
            cfg = store.get_config()
        except Exception:
            cfg = {}

        # ----------------------------------------------------------------
        # 6. Compute hash of the ORIGINAL (pre-shrink) output.
        #    The hash identifies the content; dedup collapses identical content.
        # ----------------------------------------------------------------
        h = store.sha256(output)

        # ----------------------------------------------------------------
        # 7. Dedup check
        # ----------------------------------------------------------------
        existing = store.seen_output(session_id, h)

        if existing is not None:
            # Pointer collapse — identical content seen before.
            pointer = (
                f"[nexum] identical to earlier {tool_name} output "
                f"(hash {h[:8]}) — omitted to save context."
            )
            try:
                # The model only ever saw the *shrunk* first occurrence (its
                # token count was recorded then), so the context actually avoided
                # is shrunk_tokens - pointer_tokens, not original - pointer.
                prior_tok = existing.get("token_count")
                if not isinstance(prior_tok, int) or prior_tok <= 0:
                    prior_tok = store.estimate_tokens(output)
                saved = prior_tok - store.estimate_tokens(pointer)
                if saved > 0:
                    # A repeated read would bill at the cache-read rate, not full
                    # price — weight the dollar-equivalent saving accordingly so
                    # the reported savings reflect actual API spend.
                    weight = float(cfg.get("dedup_cache_weight", 0.1))
                    effective = max(0, round(saved * weight))
                    # Gated on the self-test: only counts if the pointer actually
                    # replaces the repeated output in the model's context.
                    _maybe_record_saving(
                        session_id, "dedup", saved, effective,
                        tool_use_id, len(pointer), len(output),
                    )
            except Exception:
                pass
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": pointer,
                }
            }
            print(json.dumps(response, sort_keys=True))
            return

        # ----------------------------------------------------------------
        # 8. New content: shrink (dedup is the authority on final output),
        #    record, and emit.
        # ----------------------------------------------------------------
        shrunk, _acted = truncate.shrink(output, cfg)

        try:
            saved = store.estimate_tokens(output) - store.estimate_tokens(shrunk)
            if saved > 0:
                # Gated on the self-test: only counts if the shrunk output
                # actually replaces the original in the model's context.
                _maybe_record_saving(
                    session_id, "truncate", saved, None,
                    tool_use_id, len(shrunk), len(output),
                )
        except Exception:
            pass

        token_count = store.estimate_tokens(shrunk)
        summary = _make_summary(output, token_count)

        store.record_output(session_id, tool_name, h, summary, token_count)

        # Additively record the INPUT signature so PreToolUse predup can
        # recognise a later identical call. Wrapped in its own try/except so
        # it can never change existing behaviour.
        try:
            tool_input = data.get("tool_input") or {}
            input_sig = store.tool_call_sig(tool_name, tool_input)
            if tool_name == "Read":
                fp = tool_input.get("file_path")
                try:
                    mtime = os.path.getmtime(fp) if (fp and os.path.exists(fp)) else None
                except Exception:
                    mtime = None
            else:
                fp = None
                mtime = None
            # Record the ORIGINAL (pre-shrink) token estimate — that is what a
            # repeat would re-inject, since PostToolUse shrink is inert on
            # current Claude Code.
            store.record_tool_call(session_id, input_sig, tool_name, store.estimate_tokens(output), fp, mtime)
        except Exception:
            pass

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": shrunk,
            }
        }
        print(json.dumps(response, sort_keys=True))

    except Exception:
        # Fail-open: never crash the Claude Code session.
        print("{}")


if __name__ == "__main__":
    main()
