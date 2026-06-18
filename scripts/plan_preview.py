"""
plan_preview.py — Nexum plan cost preview CLI.

Parses a nexum plan file, maps each step's route to a model tier, and prints
a projected cost estimate per tier and total against an all-opus baseline.
This lets /nx-build show savings before dispatching any work.

Usage:
    python3 plan_preview.py --plan <path> [--session <id>]

Stdlib only. Fail-open: errors in main() degrade to a message and exit 0.
"""

import argparse
import os
import re
import sys
from typing import List

# ---------------------------------------------------------------------------
# sys.path: ensure scripts/ dir is importable as "import store"
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402


# ---------------------------------------------------------------------------
# Route → tier mapping
# ---------------------------------------------------------------------------
ROUTE_TIER = {
    "mechanical": "haiku",
    "standard": "sonnet",
    "needs-strong": "opus",
}


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------

def parse_plan_steps(text: str) -> List[dict]:
    """Parse a nexum plan markdown and return steps with index, title, and route.

    Scans for ``### Step <N>: <title>`` headers and reads the first
    ``- route: <value>`` line that follows. Returns a list of
    ``{"index": int, "title": str, "route": str}`` in file order.
    Only real ``### Step`` headers are parsed; route-rubric examples
    inside ``|``-quoted table cells are ignored.
    """
    steps = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Match ### Step N: title
        m = re.match(r'^###\s+Step\s+(\d+):\s+(.+)', line)
        if m:
            index = int(m.group(1))
            title = m.group(2).strip()
            # Scan forward for the route line (first occurrence within the block)
            route = "standard"
            for j in range(i + 1, min(i + 30, len(lines))):
                next_line = lines[j]
                # Stop at the next ### header (new step)
                if re.match(r'^###\s+Step\s+\d+:', next_line):
                    break
                # Only match a plain list item (not inside a table cell)
                rm = re.match(r'^- route:\s+(\S+)', next_line)
                if rm:
                    raw_route = rm.group(1).rstrip(',;|')
                    # Map to known routes; default to "standard"
                    if raw_route in ROUTE_TIER:
                        route = raw_route
                    break
            steps.append({"index": index, "title": title, "route": route})
        i += 1
    return steps


# ---------------------------------------------------------------------------
# Preview builder
# ---------------------------------------------------------------------------

def build_preview(steps: list, cfg: dict) -> str:
    """Build the plan cost preview string.

    For each step: cost = in_tok/1e6 * price_in + out_tok/1e6 * price_out.
    Aggregate per tier; compute baseline at opus rates.
    Returns a human-readable string starting with "[nexum] Plan cost preview".
    """
    if not steps:
        return "[nexum] No steps found in plan."

    in_tok = int(cfg.get("plan_preview_input_tok_per_step", 8000))
    out_tok = int(cfg.get("plan_preview_output_tok_per_step", 2000))

    opus_in, opus_out = store.PRICING["opus"]

    # Per-tier aggregation
    # tier -> {count, in_tok_total, out_tok_total, actual_cost, baseline_cost}
    tiers: dict = {}

    for step in steps:
        route = step.get("route", "standard")
        tier = ROUTE_TIER.get(route, "sonnet")
        price_in, price_out = store.PRICING.get(tier, store.PRICING["sonnet"])

        step_actual = in_tok / 1e6 * price_in + out_tok / 1e6 * price_out
        step_baseline = in_tok / 1e6 * opus_in + out_tok / 1e6 * opus_out

        if tier not in tiers:
            tiers[tier] = {
                "count": 0,
                "in_tok": 0,
                "out_tok": 0,
                "actual": 0.0,
                "baseline": 0.0,
            }
        tiers[tier]["count"] += 1
        tiers[tier]["in_tok"] += in_tok
        tiers[tier]["out_tok"] += out_tok
        tiers[tier]["actual"] += step_actual
        tiers[tier]["baseline"] += step_baseline

    total_actual = sum(t["actual"] for t in tiers.values())
    total_baseline = sum(t["baseline"] for t in tiers.values())
    saved = total_baseline - total_actual
    pct = (saved / total_baseline * 100) if total_baseline > 0 else 0.0

    lines = []
    lines.append("[nexum] Plan cost preview (estimate)")
    lines.append(f"  Steps: {len(steps)}  |  Per-step heuristic: {in_tok:,} in / {out_tok:,} out tokens")
    lines.append(f"  Note: token counts are a per-step heuristic, not measured usage.")
    lines.append("")
    lines.append(
        f"  {'Tier':<12} {'Steps':>6} {'Input tok':>12} {'Output tok':>12} "
        f"{'Actual $':>10} {'Baseline $':>12}"
    )
    lines.append("  " + "-" * 68)
    for tier in ("haiku", "sonnet", "opus"):
        if tier not in tiers:
            continue
        t = tiers[tier]
        lines.append(
            f"  {tier:<12} {t['count']:>6} {t['in_tok']:>12,} {t['out_tok']:>12,} "
            f"${t['actual']:>9.4f} ${t['baseline']:>11.4f}"
        )
    lines.append("  " + "-" * 68)
    lines.append(
        f"  {'TOTAL':<12} {len(steps):>6} "
        f"{sum(t['in_tok'] for t in tiers.values()):>12,} "
        f"{sum(t['out_tok'] for t in tiers.values()):>12,} "
        f"${total_actual:>9.4f} ${total_baseline:>11.4f}"
    )
    lines.append("")
    lines.append(
        f"Projected: ${total_actual:.4f} vs all-opus ${total_baseline:.4f} "
        f"— saves ${saved:.4f} ({pct:.1f}%)"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI: parse a plan file and print the cost preview."""
    try:
        parser = argparse.ArgumentParser(
            prog="plan_preview.py",
            description="Nexum plan cost preview — projected cost vs all-opus baseline.",
        )
        parser.add_argument("--plan", required=True, help="Path to the nexum plan file.")
        parser.add_argument("--session", default=None, help="Session ID (unused, accepted for parity).")
        args = parser.parse_args()

        try:
            with open(args.plan, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception:
            print(f"[nexum] No plan file at {args.plan}.")
            return

        cfg = store.get_config()
        print(build_preview(parse_plan_steps(text), cfg))
    except Exception:
        # Fail-open
        pass


if __name__ == "__main__":
    main()
