"""Turn evaluation/run_eval.py's result rows into the thesis's headline
numbers -- TASK_evaluation.md section 3.

Reads one or more JSONL result files (so a resumed/multi-session run's
files can all be analyzed together) and prints:
- effectiveness overall, and per taxonomy category (confirmed-only and
  confirmed+candidate denominators, per DEPENDENCY_FAILURE_TAXONOMY.md §3);
- grounded vs. model-proposed among accepted fixes, with the confidence
  distribution;
- how many "failures" were actually MAX_STEPS exhaustion;
- the cross-file "fixed for free" count (the repo-scale finding).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict


def load_rows(paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def _pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a (0 in denominator)"
    return f"{100 * numerator / denominator:.1f}% ({numerator}/{denominator})"


def analyze(rows: list[dict]) -> str:
    lines = []

    discovered = [r for r in rows if r.get("discovered")]
    not_discovered = [r for r in rows if not r.get("discovered")]

    lines.append(f"Total labelled notebooks in result set: {len(rows)}")
    lines.append(f"  discovered and attempted: {len(discovered)}")
    lines.append(f"  not discovered (missing/renamed/cloned repo changed since snapshot, or clone/run error): {len(not_discovered)}")
    lines.append("")

    # --- Effectiveness overall, on both denominators ---
    confirmed = [r for r in discovered if r["taxonomy_tier"] == "confirmed"]
    confirmed_and_candidate = [r for r in discovered if r["taxonomy_tier"] in ("confirmed", "candidate")]

    lines.append("Effectiveness overall:")
    lines.append(f"  confirmed only:            {_pct(sum(r['fixed'] for r in confirmed), len(confirmed))}")
    lines.append(f"  confirmed + candidate:     {_pct(sum(r['fixed'] for r in confirmed_and_candidate), len(confirmed_and_candidate))}")
    lines.append("")

    # --- Effectiveness per category ---
    lines.append("Effectiveness per taxonomy category (confirmed+candidate denominator):")
    by_category: dict[str, list[dict]] = defaultdict(list)
    for r in confirmed_and_candidate:
        by_category[r["taxonomy_category"]].append(r)
    for category in "ABCDE":
        rows_in_cat = by_category.get(category, [])
        if not rows_in_cat:
            lines.append(f"  {category}: no labelled cases")
            continue
        fixed_n = sum(r["fixed"] for r in rows_in_cat)
        lines.append(f"  {category}: {_pct(fixed_n, len(rows_in_cat))}")
    lines.append("")

    # --- Grounded vs. model-proposed, confidence distribution ---
    fixed_rows = [r for r in discovered if r.get("fixed")]
    grounded = 0
    proposed = 0
    confidence_counts: Counter = Counter()
    for r in fixed_rows:
        fix_actions = r.get("fix_actions") or []
        accepted = next((a for a in reversed(fix_actions) if a.get("verification") == "verified"), None)
        if accepted is None:
            continue
        if accepted["grounding"] == "metadata_grounded":
            grounded += 1
        elif accepted["grounding"] == "llm_proposed":
            proposed += 1
        if accepted.get("confidence"):
            confidence_counts[accepted["confidence"]] += 1

    lines.append("Accepted-fix grounding (the transparency contribution, measured):")
    lines.append(f"  metadata_grounded: {grounded}")
    lines.append(f"  llm_proposed:      {proposed}")
    lines.append("  confidence distribution:")
    for level in ("high", "medium-high", "medium", "low"):
        if confidence_counts.get(level):
            lines.append(f"    {level}: {confidence_counts[level]}")
    lines.append("")

    # --- Cap-limited failures ---
    not_fixed = [r for r in discovered if not r.get("fixed")]
    cap_hit = [r for r in not_fixed if r.get("max_steps_hit")]
    lines.append("Cap-limited vs. genuine failures (of discovered-but-not-fixed notebooks):")
    lines.append(f"  not fixed, MAX_STEPS exhausted (ran out of budget, not proven unfixable): {len(cap_hit)}")
    lines.append(f"  not fixed, genuinely stopped early (model gave up, or no tool call left): {len(not_fixed) - len(cap_hit)}")
    lines.append("")

    # --- Cross-file interaction (the repo-scale finding) ---
    free_passes = [r for r in discovered if r.get("cross_file_free_pass")]
    lines.append("Cross-file interaction (repo-scale finding):")
    lines.append(f"  notebooks that passed without any agent action, in a repo where another file was genuinely fixed: {len(free_passes)}")
    if free_passes:
        repos_with_free_pass = sorted({r["repository"] for r in free_passes})
        lines.append(f"  across {len(repos_with_free_pass)} distinct repositories")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_files", nargs="+", help="one or more .jsonl result files from run_eval.py")
    args = parser.parse_args()

    rows = load_rows(args.result_files)
    print(analyze(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
