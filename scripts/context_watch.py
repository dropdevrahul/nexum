"""
context_watch.py — Nexum UserPromptSubmit hook.

Two responsibilities:
  (a) Compaction prompt: warn the user when the running token estimate for the
      session crosses compaction_threshold_tokens (emits a systemMessage once
      per window).
  (b) Intent-change guard: detect when the user has shifted tasks mid-session
      (e.g. fix→feature) and block with a helpful message, allowing bypass via
      the word "continue".

Hook contract:
  stdin:  {"session_id": "...", "prompt": "...", ...}
  stdout: {} (allow) | {"systemMessage": "..."} (allow + warn)
           | {"decision": "block", "reason": "..."}
Fail-open: any exception → print {} and exit 0.
"""

from __future__ import annotations

import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Bootstrap: make the scripts/ directory importable regardless of cwd
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402 — must be after sys.path tweak


# ---------------------------------------------------------------------------
# Stopwords — common English words that carry no topical signal
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "its", "be", "as", "up",
    "are", "was", "were", "has", "have", "had", "do", "does", "did",
    "not", "no", "so", "if", "then", "that", "this", "these", "those",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they",
    "them", "his", "her", "their", "what", "which", "who", "how", "when",
    "where", "why", "can", "will", "would", "could", "should", "may",
    "might", "must", "shall", "about", "also", "into", "than", "more",
    "some", "out", "there", "here", "all", "just", "get", "got",
    "make", "made", "use", "used", "s", "t", "re", "ll", "ve",
})

# Task-type keyword mapping → canonical task type
_TASK_TYPE_MAP = {
    "fix":       "fix",
    "bug":       "fix",
    "error":     "fix",
    "fixing":    "fix",
    "fixed":     "fix",
    "debug":     "fix",
    "debugging": "fix",
    "broken":    "fix",
    "issue":     "fix",
    "crash":     "fix",
    "exception": "fix",

    "add":         "feature",
    "implement":   "feature",
    "feature":     "feature",
    "new":         "feature",
    "build":       "feature",
    "create":      "feature",
    "introduce":   "feature",
    "integrat":    "feature",  # integrate / integration
    "billing":     "feature",
    "invoic":      "feature",  # invoice / invoices

    "refactor":    "refactor",
    "refactoring": "refactor",
    "restructure": "refactor",
    "reorganize":  "refactor",
    "clean":       "refactor",
    "cleanup":     "refactor",

    "test":    "test",
    "tests":   "test",
    "testing": "test",
    "spec":    "test",
    "unit":    "test",

    "docs":          "docs",
    "doc":           "docs",
    "documentation": "docs",
    "document":      "docs",
    "readme":        "docs",
    "comment":       "docs",
    "comments":      "docs",
}

# Regex that matches word characters (used for tokenisation)
_WORD_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------

def _derive_task_type(words: set) -> str | None:
    """Return the canonical task type found in *words*, or None.

    Words are scanned in sorted order so the result is deterministic across
    process runs. Iterating a set directly would follow hash order, which is
    randomized per-process (PYTHONHASHSEED); for a prompt mentioning more than
    one task type that made the derived type — and therefore the intent-guard
    block/allow decision — flip between otherwise-identical runs.
    """
    for word in sorted(words):
        # Try exact match first, then prefix match for stemmed forms
        if word in _TASK_TYPE_MAP:
            return _TASK_TYPE_MAP[word]
        # Prefix match handles light stemming (e.g. "integrate" → "integrat")
        for prefix, task_type in _TASK_TYPE_MAP.items():
            if len(prefix) >= 5 and word.startswith(prefix):
                return task_type
    return None


def _signature(prompt: str) -> tuple[set, str | None]:
    """Return (keyword_set, task_type) for *prompt*.

    keyword_set: lowercased words minus stopwords.
    task_type:   canonical task type string or None.
    """
    words = set(_WORD_RE.findall(prompt.lower()))
    keywords = words - _STOPWORDS
    task_type = _derive_task_type(keywords)
    return keywords, task_type


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets.  Returns 0.0 if both empty."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _pack_sig(keywords: set, task_type: str | None) -> str:
    """Serialise a signature to a storable string."""
    parts = sorted(keywords)
    if task_type:
        parts = [f"__type__:{task_type}"] + parts
    return json.dumps(parts)


def _unpack_sig(raw: str) -> tuple[set, str | None]:
    """Deserialise a signature from a stored string."""
    try:
        parts = json.loads(raw)
    except Exception:
        return set(), None
    task_type = None
    keywords = set()
    for p in parts:
        if p.startswith("__type__:"):
            task_type = p[len("__type__:"):]
        else:
            keywords.add(p)
    return keywords, task_type


# ---------------------------------------------------------------------------
# Session KV wrappers (thin sugar so call-sites read clearly)
# ---------------------------------------------------------------------------

def _kv_get(session_id: str, key: str) -> str | None:
    return store.get_flag(session_id, key)


def _kv_set(session_id: str, key: str, value: str) -> None:
    store.set_flag(session_id, key, value)


# ---------------------------------------------------------------------------
# Token accumulation
# ---------------------------------------------------------------------------

def _accumulate_tokens(session_id: str, prompt: str) -> int:
    """Add this prompt's token estimate to the running session total.

    Returns the new running total.
    """
    delta = store.estimate_tokens(prompt)
    raw = _kv_get(session_id, "token_total")
    try:
        total = int(raw) + delta if raw is not None else delta
    except (TypeError, ValueError):
        total = delta
    _kv_set(session_id, "token_total", str(total))
    return total


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------

def _allow(**extra) -> dict:
    return {**extra}  # {} or {"systemMessage": "..."}


def _block(reason: str) -> dict:
    return {"decision": "block", "reason": reason}


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def _handle(data: dict) -> dict:
    session_id = data.get("session_id") or "_nosession"
    prompt: str = data.get("prompt") or ""
    prompt_stripped = prompt.strip()

    cfg = store.get_config()

    # ------------------------------------------------------------------ #
    # Edge case: empty prompt → allow immediately, no state changes
    # ------------------------------------------------------------------ #
    if not prompt_stripped:
        return _allow()

    # ------------------------------------------------------------------ #
    # (a) Token accumulation + compaction prompt
    # ------------------------------------------------------------------ #
    token_total = _accumulate_tokens(session_id, prompt_stripped)
    compaction_threshold = int(cfg.get("compaction_threshold_tokens", 120000))
    system_message: str | None = None

    if token_total >= compaction_threshold:
        already_warned = _kv_get(session_id, "compaction_warned")
        if not already_warned:
            k_tokens = round(token_total / 1000)
            system_message = (
                f"[nexum] Context is large (~{k_tokens}k tokens). "
                "Consider /compact to reduce cost."
            )
            _kv_set(session_id, "compaction_warned", "1")

    # ------------------------------------------------------------------ #
    # (b) Intent-change guard
    # ------------------------------------------------------------------ #
    intent_guard_enabled = cfg.get("intent_guard_enabled", True)
    similarity_threshold = float(cfg.get("intent_similarity_threshold", 0.25))

    new_keywords, new_task_type = _signature(prompt_stripped)

    # "continue" bypass handling — must be checked BEFORE guard logic
    is_continue = prompt_stripped.lower() == "continue"

    if is_continue:
        # User acknowledged the block; set bypass flag and allow.
        # Do NOT update the task yet — that happens after a real prompt clears
        # bypass_intent; doing it here would adopt "continue" as the task.
        _kv_set(session_id, "bypass_intent", "1")
        # Clear the last-blocked signature so the next real prompt can reset properly
        _kv_set(session_id, "pending_sig", "")
        # Emit system message if one was queued, then allow
        if system_message:
            return _allow(systemMessage=system_message)
        return _allow()

    if intent_guard_enabled:
        raw_task = store.get_session_task(session_id)

        if raw_task is None:
            # First prompt in the session: store and allow
            store.set_session_task(session_id, _pack_sig(new_keywords, new_task_type))
        else:
            old_keywords, old_task_type = _unpack_sig(raw_task)
            similarity = _jaccard(new_keywords, old_keywords)

            bypass = _kv_get(session_id, "bypass_intent")
            # "pending_sig" is the signature we blocked on; used to adopt after bypass
            pending_raw = _kv_get(session_id, "pending_sig") or ""

            # Determine whether a task-type change occurred
            task_type_changed = (
                old_task_type is not None
                and new_task_type is not None
                and old_task_type != new_task_type
            )

            should_block = (
                task_type_changed
                and similarity < similarity_threshold
                and not bypass
            )

            if should_block:
                # Store the pending signature so "continue" can adopt it later
                _kv_set(
                    session_id,
                    "pending_sig",
                    _pack_sig(new_keywords, new_task_type),
                )
                reason = (
                    f"[nexum] This looks like a new task ({old_task_type}->{new_task_type}). "
                    "A fresh session gives cleaner, cheaper context. "
                    "Reply 'continue' to proceed here."
                )
                return _block(reason)
            else:
                # Allowed: update the stored task signature
                if bypass:
                    # Clear bypass; adopt the pending (or current) signature
                    adopt_raw = pending_raw if pending_raw else _pack_sig(new_keywords, new_task_type)
                    store.set_session_task(session_id, adopt_raw)
                    _kv_set(session_id, "bypass_intent", "")
                    _kv_set(session_id, "pending_sig", "")
                else:
                    store.set_session_task(session_id, _pack_sig(new_keywords, new_task_type))
    else:
        # Guard disabled: just update the task signature
        store.set_session_task(session_id, _pack_sig(new_keywords, new_task_type))

    # Allow — possibly with a compaction systemMessage
    if system_message:
        return _allow(systemMessage=system_message)
    return _allow()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
        result = _handle(data)
        print(json.dumps(result, sort_keys=True))
    except Exception:
        print("{}")
        sys.exit(0)


if __name__ == "__main__":
    main()
