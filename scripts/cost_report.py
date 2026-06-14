"""
cost_report.py — Nexum cost report CLI.

Usage:
    python3 cost_report.py [--session <id>]

Reads usage rows from the nexum store and prints:
  - Actual cost (using PRICING for each model)
  - All-opus baseline cost (same tokens priced at opus rates)
  - Savings (baseline - actual)
  - Per-model breakdown

Token cost formula:
    input cost  = input_tok  / 1_000_000 * price_in
    output cost = output_tok / 1_000_000 * price_out
    cache_read  = cache_read_tok / 1_000_000 * price_in * 0.1

v1 data source: store usage rows only.
If CLAUDE_CODE_ENABLE_TELEMETRY is set, usage data may also be available
via OTel metrics (nexum.input_tokens, nexum.output_tokens, nexum.cache_read_tokens)
— but in v1 we do not build a collector; set that env var and instrument
store.add_usage() at call sites to record data from the OTel stream.
"""

import argparse
import sys
import os

# Allow running as `python3 scripts/cost_report.py` with the scripts dir in path.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store


# ---------------------------------------------------------------------------
# Cost computation helpers
# ---------------------------------------------------------------------------

def _model_key(model: str) -> str:
    """Normalise a model string to one of the PRICING keys, or return as-is."""
    m = model.lower()
    for key in ("opus", "sonnet", "haiku"):
        if key in m:
            return key
    return m  # unknown model — caller handles missing key


def _row_cost(row: dict, price_in: float, price_out: float) -> float:
    """Compute cost in USD for a single usage row at the given per-1M rates."""
    cost = (
        row["input_tok"]  / 1_000_000 * price_in
        + row["output_tok"] / 1_000_000 * price_out
        + row.get("cache_read_tok", 0) / 1_000_000 * price_in * 0.1
    )
    return cost


# ---------------------------------------------------------------------------
# Main report logic
# ---------------------------------------------------------------------------

def build_report(rows: list) -> str:
    """Return the formatted cost report as a string."""
    if not rows:
        return "[nexum] No usage rows found."

    opus_in, opus_out = store.PRICING["opus"]

    # Aggregate by model key
    # model_key -> {input_tok, output_tok, cache_read_tok, actual_cost}
    breakdown: dict = {}
    total_actual = 0.0
    total_baseline = 0.0

    # Track whether any shipped-token data exists (future tagging placeholder)
    # In v1 we have input_tok / output_tok / cache_read_tok — no separate
    # "shipped token" field.  If such a field were added to the schema, token
    # yield (shipped / input) could be computed here.
    has_shipped_tokens = False  # noqa: always False in v1

    for row in rows:
        key = _model_key(row.get("model", "unknown"))

        if key in store.PRICING:
            p_in, p_out = store.PRICING[key]
        else:
            # Unknown model: treat as sonnet (warn inline)
            p_in, p_out = store.PRICING["sonnet"]
            key = f"{key}(unknown→sonnet rates)"

        actual = _row_cost(row, p_in, p_out)
        baseline = _row_cost(row, opus_in, opus_out)

        total_actual += actual
        total_baseline += baseline

        if key not in breakdown:
            breakdown[key] = {
                "input_tok": 0,
                "output_tok": 0,
                "cache_read_tok": 0,
                "actual_cost": 0.0,
                "baseline_cost": 0.0,
                "rows": 0,
            }
        bd = breakdown[key]
        bd["input_tok"]      += row["input_tok"]
        bd["output_tok"]     += row["output_tok"]
        bd["cache_read_tok"] += row.get("cache_read_tok", 0)
        bd["actual_cost"]    += actual
        bd["baseline_cost"]  += baseline
        bd["rows"]           += 1

    saved = total_baseline - total_actual

    lines = []
    lines.append("[nexum] Cost report")
    lines.append("=" * 48)
    lines.append(f"  Actual cost:          ${total_actual:>10.4f}")
    lines.append(f"  All-opus baseline:    ${total_baseline:>10.4f}")
    lines.append(f"  Saved vs opus:        ${saved:>10.4f}")
    lines.append("")
    lines.append("Per-model breakdown:")
    lines.append(f"  {'Model':<20} {'Input tok':>12} {'Output tok':>12} "
                 f"{'Cache-R tok':>12} {'Actual $':>10} {'Baseline $':>10}")
    lines.append("  " + "-" * 80)

    for key in sorted(breakdown):
        bd = breakdown[key]
        lines.append(
            f"  {key:<20} {bd['input_tok']:>12,} {bd['output_tok']:>12,} "
            f"{bd['cache_read_tok']:>12,} "
            f"${bd['actual_cost']:>9.4f} ${bd['baseline_cost']:>9.4f}"
        )

    lines.append("")
    if has_shipped_tokens:
        pass  # future: print token yield per model
    else:
        lines.append("[nexum] Note: token yield needs shipped-token tagging "
                     "(no shipped-token field in v1 usage rows).")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cost_report.py",
        description="Nexum cost report — actual vs all-opus baseline.",
    )
    parser.add_argument(
        "--session",
        metavar="ID",
        default=None,
        help="Filter to a specific session id (omit for all sessions).",
    )
    args = parser.parse_args()

    rows = store.usage_rows(session_id=args.session)
    print(build_report(rows))


if __name__ == "__main__":
    main()
