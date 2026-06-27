#!/usr/bin/env python3
"""
report.py — Nexum session digest (/nx-report).

Deterministic, no-LLM analytics over the data nexum already records:

  * Wasted-context analysis — per-file read/edit accounting from file_activity:
    which files were read into context but never edited (wasted tokens), a waste
    ratio, an efficiency grade, and concrete "drop X to save ~N tokens" picks.
  * Cost summary — the actual-vs-all-opus tiering breakdown and Claude Code's
    own metered, cache-accurate total (reused from cost_report).

CLI:
    python3 report.py [--session <id>]

All computation is local and rule-based; the command body just presents this.
"""

import argparse
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store        # noqa: E402
import cost_report  # noqa: E402

# Efficiency grade by waste ratio (wasted_tokens / total_read_tokens).
_GRADE_BANDS = [
    (0.10, "S"), (0.20, "A"), (0.35, "B"),
    (0.50, "C"), (0.70, "D"), (1.01, "F"),
]


def _grade(waste_ratio: float) -> str:
    for ceiling, letter in _GRADE_BANDS:
        if waste_ratio <= ceiling:
            return letter
    return "F"


def _usefulness(row: dict) -> float:
    """+3 per edit, +0.5 per re-read (beyond the first), +1 if ever partial."""
    reads = int(row.get("reads") or 0)
    edits = int(row.get("edits") or 0)
    partial = int(row.get("partial_reads") or 0)
    return 3 * edits + 0.5 * max(0, reads - 1) + (1 if partial > 0 else 0)


def _fmt_tok(n: int) -> str:
    n = int(n or 0)
    return str(n) if n < 1000 else f"{n / 1000:.1f}k"


def build_waste_section(rows: list) -> str:
    if not rows:
        return ("Wasted-context analysis:\n"
                "  No file reads recorded yet "
                "(file_activity is empty for this scope).")

    total_read = sum(int(r.get("tokens_read") or 0) for r in rows)
    wasted_rows = [r for r in rows if int(r.get("edits") or 0) == 0
                   and int(r.get("tokens_read") or 0) > 0]
    wasted_tok = sum(int(r.get("tokens_read") or 0) for r in wasted_rows)
    ratio = (wasted_tok / total_read) if total_read else 0.0

    lines = []
    lines.append("Wasted-context analysis:")
    lines.append(f"  Tokens read into context:   {total_read:>10,}")
    lines.append(f"  Spent on never-edited files:{wasted_tok:>10,}  "
                 f"({ratio * 100:.0f}% wasted)")
    lines.append(f"  Efficiency grade:           {_grade(ratio):>10}")
    lines.append("")
    lines.append(f"  {'File':<48} {'Read':>5} {'Edit':>5} {'Tok':>8}  Verdict")
    lines.append("  " + "-" * 82)
    for r in sorted(rows, key=lambda x: int(x.get("tokens_read") or 0), reverse=True)[:12]:
        fp = str(r.get("file_path") or "?")
        disp = fp if len(fp) <= 48 else "…" + fp[-47:]
        verdict = "useful" if int(r.get("edits") or 0) > 0 else (
            "WASTED" if int(r.get("tokens_read") or 0) > 0 else "—")
        lines.append(
            f"  {disp:<48} {int(r.get('reads') or 0):>5} "
            f"{int(r.get('edits') or 0):>5} {_fmt_tok(r.get('tokens_read')):>8}  {verdict}"
        )

    if wasted_rows:
        lines.append("")
        lines.append("  Suggestions — stop loading these (read, never edited):")
        for r in sorted(wasted_rows, key=lambda x: int(x.get("tokens_read") or 0),
                        reverse=True)[:5]:
            fp = str(r.get("file_path") or "?")
            lines.append(f"    drop {fp} → save ~{_fmt_tok(r.get('tokens_read'))} tokens")
    return "\n".join(lines)


# Savings sources, classified by how trustworthy the number is. PreToolUse
# levers are honored by current Claude Code; PostToolUse output replacement is
# not (updatedToolOutput is ignored for built-in tools — anthropics/claude-code
# #65403), so dedup/truncate are reported separately and never folded into the
# realized headline.
_SAVINGS_BOUNDED = {"read_guard", "grep_narrow"}
_SAVINGS_THEORETICAL = {"dedup", "truncate"}
_SAVINGS_LABEL = {
    "predup": "repeat tool calls denied",
    "read_guard": "large reads capped",
    "grep_narrow": "broad searches bounded",
    "dedup": "duplicate outputs (PostToolUse)",
    "truncate": "oversized outputs (PostToolUse)",
}


def build_savings_section(by_source: dict) -> str:
    """Render savings split into realized / bounded / theoretical buckets.

    Honest by construction: only *realized* (PreToolUse, measured) tokens go in
    the headline; *bounded* interventions are counted but carry no token claim;
    *theoretical* PostToolUse shrink is shown with the upstream-bug caveat and
    never summed into the realized total.
    """
    realized, bounded, theoretical = {}, {}, {}
    for src, agg in (by_source or {}).items():
        if src in _SAVINGS_THEORETICAL:
            theoretical[src] = agg
        elif src in _SAVINGS_BOUNDED:
            bounded[src] = agg
        else:
            realized[src] = agg

    lines = ["Savings analysis:"]

    realized_tok = sum(int(a.get("effective_tok") or 0) for a in realized.values())
    lines.append("  Realized (PreToolUse — actually removed from context):")
    if realized:
        for src in sorted(realized):
            a = realized[src]
            lines.append(
                f"    {_SAVINGS_LABEL.get(src, src):<32} "
                f"{a.get('count', 0):>4} calls  ~{_fmt_tok(a.get('effective_tok'))} tok saved"
            )
        lines.append(f"    {'TOTAL realized':<32} {'':>4}        ~{_fmt_tok(realized_tok)} tok")
    else:
        lines.append("    none recorded this scope")

    lines.append("  Bounded interventions (output capped; exact saving unknowable):")
    if bounded:
        for src in sorted(bounded):
            a = bounded[src]
            lines.append(f"    {_SAVINGS_LABEL.get(src, src):<32} {a.get('count', 0):>4} ×")
    else:
        lines.append("    none recorded this scope")

    lines.append(
        "  Theoretical (PostToolUse shrink — INERT on current Claude Code;\n"
        "  updatedToolOutput is ignored for built-in tools, tracking #65403):"
    )
    if theoretical:
        for src in sorted(theoretical):
            a = theoretical[src]
            lines.append(
                f"    {_SAVINGS_LABEL.get(src, src):<32} "
                f"{a.get('count', 0):>4} ×  ~{_fmt_tok(a.get('effective_tok'))} tok "
                f"would save once the field is honored"
            )
    else:
        lines.append("    nothing recorded — PostToolUse shrink contributes 0 today")
    return "\n".join(lines)


def build_digest(session_id=None) -> str:
    parts = ["[nexum] Session report", "=" * 48, ""]
    # Cost (reuses cost_report's deterministic builders).
    parts.append(cost_report.build_report(store.usage_rows(session_id=session_id)))
    parts.append("")
    parts.append(cost_report.build_metered_section(store.session_cost_rows(session_id=session_id)))
    parts.append("")
    parts.append(build_savings_section(store.savings_by_source(session_id=session_id)))
    parts.append("")
    parts.append(build_waste_section(store.file_activity_rows(session_id=session_id)))
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="report.py", description="Nexum session digest: cost + wasted context."
    )
    parser.add_argument("--session", metavar="ID", default=None,
                        help="Filter to a session id (omit for all sessions).")
    args = parser.parse_args()
    print(build_digest(session_id=args.session))


if __name__ == "__main__":
    main()
