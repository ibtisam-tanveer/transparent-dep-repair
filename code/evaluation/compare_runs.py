"""Side-by-side comparison of result files from the same notebooks (e.g. a lever on vs off).

    python evaluation/compare_runs.py evaluation/results/era_off6.jsonl evaluation/results/era_on7.jsonl

Prints, per notebook: the outcome and where it ended in every run, agent steps / model calls,
and (when present) the era-resolution record -- plus totals. Read-only; reports, does not judge.
"""

from __future__ import annotations

import json
import os
import sys


def load(path: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                rows[r["repository"].split("/")[-1]] = r
    return rows


def outcome(r: dict | None) -> str:
    if r is None:
        return "-"
    if r.get("fixed"):
        return "FIXED"
    where = r.get("runtime_observed_error") or r.get("error") or r.get("stop_reason") or "?"
    return f"{r.get('stop_reason', '?')}: {where}"[:90]


def _setup_text(setup: dict) -> str:
    # Rows written before the key fix kept the per-install counts at the top level (applied = a count).
    declared = setup.get("declared_installs") or {k: setup.get(k, 0) for k in ("installs", "applied", "fallback")}
    n = declared.get("installs", 0)
    if not n:
        return "no declared pip installs to constrain"
    return f"declared pip installs {n}: {declared.get('applied', 0)} constrained, {declared.get('fallback', 0)} fell back"


def era_note(r: dict | None) -> str:
    if r is None or "era_resolve" not in (r.get("levers") or "").split(","):
        return ""
    setup, agent = r.get("era_resolution_setup") or {}, r.get("era_resolution_agent") or {}
    return (f"date {r.get('era_cutoff_date')}; env build "
            f"{_setup_text(setup)}; "
            f"agent installs {agent.get('installs', 0)}: {agent.get('applied', 0)} constrained, "
            f"{agent.get('fallback', 0)} fell back, {agent.get('not_applied', 0)} n/a")


def main(paths: list[str]) -> int:
    runs = [(os.path.splitext(os.path.basename(p))[0], load(p)) for p in paths]
    names = sorted({n for _, rows in runs for n in rows})
    for name in names:
        print(f"\n{name}")
        for tag, rows in runs:
            r = rows.get(name)
            print(f"  [{tag}] {outcome(r)}   (steps {r.get('num_agent_steps') if r else '-'}, calls {r.get('llm_calls') if r else '-'})")
            note = era_note(r)
            if note:
                print(f"      era: {note}")
    print("\nTotals")
    for tag, rows in runs:
        fixed = sum(1 for r in rows.values() if r.get("fixed"))
        steps = sum(r.get("num_agent_steps") or 0 for r in rows.values())
        calls = sum(r.get("llm_calls") or 0 for r in rows.values())
        capped = sum(1 for r in rows.values() if r.get("max_steps_hit"))
        print(f"  [{tag}] fixed {fixed}/{len(rows)}; agent steps {steps}; model calls {calls}; hit the step cap {capped}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
