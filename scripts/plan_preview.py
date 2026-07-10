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
import json
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

def _parse_files_field(value: str) -> List[str]:
    """Split a step's ``files:`` value into a list of paths.

    Returns [] for ``none`` / empty. Splits on commas and trims whitespace.
    """
    value = (value or "").strip()
    if not value or value.lower() == "none":
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def parse_plan_steps(text: str) -> List[dict]:
    """Parse a nexum plan markdown and return steps with index, title, route, files.

    Scans for ``### Step <N>: <title>`` headers and reads the first
    ``- route: <value>`` and ``- files: <value>`` lines that follow within the
    step block. Returns a list of
    ``{"index": int, "title": str, "route": str, "files": [str, ...]}`` in file
    order. Only real ``### Step`` headers are parsed; route-rubric examples
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
            # Scan forward for the route + files lines within this step block.
            route = "standard"
            files: List[str] = []
            for j in range(i + 1, min(i + 30, len(lines))):
                next_line = lines[j]
                # Stop at the next ### header (new step)
                if re.match(r'^###\s+Step\s+\d+:', next_line):
                    break
                # Only match plain list items (not inside a table cell)
                rm = re.match(r'^- route:\s+(\S+)', next_line)
                if rm:
                    raw_route = rm.group(1).rstrip(',;|')
                    if raw_route in ROUTE_TIER:
                        route = raw_route
                    continue
                fm = re.match(r'^- files:\s+(.+)', next_line)
                if fm:
                    files = _parse_files_field(fm.group(1))
                    continue
            steps.append({"index": index, "title": title, "route": route, "files": files})
        i += 1
    return steps


def estimate_step_tokens(files: List[str], root: str, base: int) -> int:
    """Estimate a step's context-token load: *base* + (file bytes ÷ 4) summed
    over the step's declared files that exist on disk.

    Missing files (to be created by the step) contribute only the base. Paths
    may be absolute or relative to *root*. Fail-open per file (an unstattable
    path is skipped).
    """
    total = int(base)
    for f in files:
        p = f if os.path.isabs(f) else os.path.join(root, f)
        try:
            if os.path.isfile(p):
                total += os.path.getsize(p) // 4
        except OSError:
            pass
    return total


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

    opus_in, opus_out = store.get_pricing()["opus"]

    # Per-tier aggregation
    # tier -> {count, in_tok_total, out_tok_total, actual_cost, baseline_cost}
    tiers: dict = {}

    for step in steps:
        route = step.get("route", "standard")
        tier = ROUTE_TIER.get(route, "sonnet")
        price_in, price_out = store.get_pricing().get(tier, store.get_pricing()["sonnet"])

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
        parser.add_argument(
            "--indices", default=None,
            help="Comma-separated step indices (in execution order) to partition "
                 "into size-aware sub-batches. When given, prints a JSON array of "
                 "sub-batches instead of the cost preview.",
        )
        parser.add_argument(
            "--root", default=None,
            help="Repo root for resolving step file paths (default: cwd).",
        )
        args = parser.parse_args()

        try:
            with open(args.plan, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception:
            if args.indices is not None:
                # Batch mode must still emit valid JSON for the caller.
                print("[]")
            else:
                print(f"[nexum] No plan file at {args.plan}.")
            return

        cfg = store.get_config()
        steps = parse_plan_steps(text)

        if args.indices is not None:
            print(_build_batches(steps, args.indices, args.root, cfg))
            return

        print(build_preview(steps, cfg))
    except Exception:
        # Fail-open
        if "--indices" in sys.argv:
            print("[]")


def _build_batches(steps: list, indices_arg: str, root: str, cfg: dict) -> str:
    """Return a JSON array of size-aware sub-batches for the requested indices.

    Estimates each requested step's context tokens (base + file bytes ÷ 4) and
    packs them with store.partition_steps_by_size, bounded by
    max_dispatch_context_tokens and max_steps_per_dispatch. Order is preserved.
    Unknown indices fall back to base-only size so they still dispatch.
    """
    root = root or os.getcwd()
    by_index = {s["index"]: s for s in steps}
    base = int(cfg.get("dispatch_step_base_tokens", 1500))
    max_size = int(cfg.get("max_dispatch_context_tokens", 50000))
    max_per = int(cfg.get("max_steps_per_dispatch", 4))

    items: List[int] = []
    sizes: List[int] = []
    for tok in (indices_arg or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            idx = int(tok)
        except ValueError:
            continue
        items.append(idx)
        step = by_index.get(idx)
        files = step.get("files", []) if step else []
        sizes.append(estimate_step_tokens(files, root, base))

    return json.dumps(
        store.partition_steps_by_size(items, sizes, max_size, max_per)
    )


if __name__ == "__main__":
    main()
