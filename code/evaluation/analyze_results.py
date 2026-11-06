"""Turn evaluation/run_eval.py's result rows into the thesis's headline
numbers -- TASK_evaluation.md section 3, reworked per PILOT_DECISIONS.md #4
to report effectiveness on an IN-SCOPE denominator.

Reads one or more JSONL result files (so a resumed/multi-session run's
files can all be analyzed together) and prints:
- what's excluded from the effectiveness denominator and why (never
  attempted/budget exhausted, too large for context, out-of-scope reason
  discovered at run time) -- each reported as its own count, never silently
  folded into "failure";
- effectiveness overall, and per taxonomy category, on that in-scope
  denominator (confirmed-only and confirmed+candidate framings, per
  DEPENDENCY_FAILURE_TAXONOMY.md §3);
- grounded vs. model-proposed among accepted fixes, with the confidence
  distribution;
- how many of the remaining in-scope non-fixes were actually MAX_STEPS
  exhaustion (inconclusive, not a proven failure);
- the cross-file "fixed for free" count (the repo-scale finding).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.runtime_reasons import reason_group  # noqa: E402


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

    # --- PILOT_DECISIONS.md #4 + TASK_fix_repo_execution.md Part B: carve
    # the non-outcomes out of `discovered` *before* computing
    # effectiveness, instead of letting them dilute the "failure" count.
    # Each row falls into at most one bucket below (all only ever apply to
    # a not-fixed row).
    budget_exhausted = [r for r in discovered if r.get("budget_exhausted")]
    too_large = [r for r in discovered if r.get("too_large_for_context")]
    quota_exhausted = [r for r in discovered if r.get("quota_exhausted")]
    local_module = [r for r in discovered if r.get("local_module_import")]
    # Found in kiste_smoke_v5: 27 covid_19_analysis notebooks ended with
    # "LLM call failed: Connection error / Request timed out" after ZERO
    # completed model calls -- the backend was unreachable (VPN/server), so
    # the tool never got a chance to try. Counting them as repair failures
    # inflated the denominator from 12 to 39. Same principle as
    # quota_exhausted: infrastructure, not an outcome. Requires llm_calls == 0
    # so a mid-run error after real work still counts as an attempt.
    llm_unreachable = [
        r for r in discovered
        if not r.get("fixed") and r.get("stop_reason") == "error" and not r.get("llm_calls")
        and not r.get("quota_exhausted") and not r.get("too_large_for_context")
    ]
    # TASK_interpreter_repair.md Part D: the repo needs an interpreter no
    # reachable build can supply (Python 2, or no build at all) -- the tool
    # never got a fair chance, so this is its own explicit residual category,
    # not a repair failure.
    interpreter_unprovisionable = [
        r for r in discovered
        if not r.get("fixed") and r.get("interpreter_unprovisionable")
        and not r.get("budget_exhausted") and not r.get("too_large_for_context")
        and not r.get("quota_exhausted") and not r.get("local_module_import")
    ]
    # TASK_containerize.md: the container itself could not be provided (image
    # build / start / copy failed) -- infrastructure, like an unreachable LLM.
    container_unavailable = [
        r for r in discovered
        if not r.get("fixed") and (r.get("container_error") or str(r.get("error", "")).startswith("container unavailable"))
    ]
    already_excluded = {
        id(r) for r in budget_exhausted + too_large + quota_exhausted + local_module
        + interpreter_unprovisionable + llm_unreachable + container_unavailable
    }
    runtime_out_of_scope = [
        r for r in discovered
        if not r.get("fixed") and id(r) not in already_excluded and r.get("runtime_observed_category") == "E"
    ]
    excluded_ids = already_excluded | {id(r) for r in runtime_out_of_scope}
    in_scope = [r for r in discovered if id(r) not in excluded_ids]

    lines.append("Excluded from the effectiveness denominator (reported separately, never counted as a failure):")
    lines.append(f"  never attempted -- this repo's time budget ran out first: {len(budget_exhausted)}")
    lines.append(f"  too large for the model's context window: {len(too_large)}")
    lines.append(f"  API key's credit/quota ran out mid-attempt: {len(quota_exhausted)}")
    lines.append(f"  ModuleNotFoundError for a repo-local module, not a missing PyPI package: {len(local_module)}")
    lines.append(f"  LLM backend unreachable (connection error / timeout, no model call ever completed): "
                 f"{len(llm_unreachable)}")
    lines.append(f"  container could not be provided (image/start/copy failed): {len(container_unavailable)}")
    lines.append(f"  interpreter unprovisionable (Python 2 / no reachable build of the declared version): "
                 f"{len(interpreter_unprovisionable)}")
    lines.append(f"  out-of-scope reason discovered at run time, reclassified as category E "
                 f"(missing data/GPU/credentials/etc., not a dependency problem): {len(runtime_out_of_scope)}")
    lines.append(f"  in scope and genuinely attempted (the effectiveness denominator below): {len(in_scope)}")
    lines.append("")

    # --- Effectiveness overall, on both denominators, in-scope only ---
    confirmed = [r for r in in_scope if r["taxonomy_tier"] == "confirmed"]
    confirmed_and_candidate = [r for r in in_scope if r["taxonomy_tier"] in ("confirmed", "candidate")]

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
    fixed_rows = [r for r in in_scope if r.get("fixed")]
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

    # --- WHY notebooks failed (TASK_next_steps.md step 1) ---
    # Out-of-scope used to be a bare count; the thesis's scope finding needs the
    # reason. Reads runtime_observed_reason (slug from runtime_reasons.py);
    # rows from before that field existed fall into "reason not recorded".
    def _reason(row: dict) -> str:
        return row.get("runtime_observed_reason") or "reason not recorded"

    def _reason_lines(title: str, group: list[dict]) -> None:
        lines.append(title)
        if not group:
            lines.append("  (none)")
            return
        for reason, n in Counter(_reason(r) for r in group).most_common():
            lines.append(f"  {reason}: {n}")

    _reason_lines("Out-of-scope (category E at run time), by observed reason:", runtime_out_of_scope)
    lines.append("")
    _reason_lines("In-scope genuine failures (not fixed), by observed reason:", [r for r in in_scope if not r.get("fixed")])
    lines.append("")

    # Where timed-out notebooks stopped ("last started code cell K of N"):
    # stuck in the first cells (a hang / immediate download) reads very
    # differently from running most of the notebook and stopping late.
    import re as _re

    timeouts = [r for r in discovered if not r.get("fixed") and "timeout" in (r.get("runtime_observed_reason") or "")]
    located = []
    for r in timeouts:
        m = _re.search(r"cell (\d+) of (\d+)", r.get("runtime_observed_progress") or "")
        if m:
            located.append((int(m.group(1)), int(m.group(2))))
    lines.append("Where timed-out notebooks stopped:")
    if not timeouts:
        lines.append("  (no timeouts)")
    else:
        lines.append(f"  timed-out notebooks: {len(timeouts)}; with a recorded position: {len(located)}")
        if located:
            early = sum(1 for k, n in located if k <= 3)
            late = sum(1 for k, n in located if n and k / n >= 0.5)
            lines.append(f"    stopped in the first 3 code cells: {early}")
            lines.append(f"    stopped in the second half of the notebook: {late}")
            lines.append("    positions (cell K of N): " + ", ".join(f"{k}/{n}" for k, n in sorted(located)))
    lines.append("")

    # The scope finding: of all failures the tool actually saw, how many are
    # dependency-repairable vs. need data / compute / network / credentials.
    # Excludes rows that say nothing about the failure itself (never attempted,
    # model backend down, context/quota wall, interpreter unobtainable).
    seen = [
        r for r in discovered
        if not (r.get("budget_exhausted") or r.get("too_large_for_context") or r.get("quota_exhausted")
                or r.get("interpreter_unprovisionable") or id(r) in {id(x) for x in llm_unreachable})
    ]
    scope = Counter()
    for r in seen:
        if r.get("fixed"):
            scope["fixed (dependency-repairable)"] += 1
        elif r.get("local_module_import"):
            scope["repo-local module / path problem"] += 1
        else:
            group = reason_group(r.get("runtime_observed_reason") or "")
            scope[{
                "dependency": "dependency failure, not fixed",
                "needs_data_compute_or_network": "needs data / compute / network / credentials",
                "external_tool": "needs an external / system tool or shell command (not a Python package)",
                "code_error": "other code error (not a dependency)",
                "local_module": "repo-local module / path problem",
            }.get(group, "no failure observed / reason not recorded")] += 1
    lines.append("Scope finding (all failures the tool saw, excluding never-attempted / backend-down / wall cases):")
    lines.append(f"  failures seen: {len(seen)}")
    for label in (
        "fixed (dependency-repairable)", "dependency failure, not fixed", "repo-local module / path problem",
        "needs data / compute / network / credentials",
        "needs an external / system tool or shell command (not a Python package)",
        "other code error (not a dependency)",
        "no failure observed / reason not recorded",
    ):
        lines.append(f"  {label}: {_pct(scope[label], len(seen))}")
    repairable = scope["fixed (dependency-repairable)"] + scope["dependency failure, not fixed"]
    lines.append(f"  => dependency-related in total (fixed + not fixed): {_pct(repairable, len(seen))}")
    lines.append("")

    # --- Working-directory resolution (TASK_prepilot_fixes.md #3) ---
    # A repo-local import failure used to end a notebook as "local module";
    # now the tool first tries the directory the notebook's relative
    # sys.path entries expect. Report what that recovered, separately.
    searched = [r for r in discovered if r.get("working_directory_searched")]
    recovered = [r for r in searched if r.get("working_directory_recovered")]
    still_local = [r for r in searched if r.get("local_module_import") and not r.get("fixed")]
    recovered_fixed = [r for r in recovered if r.get("fixed")]
    lines.append("Repo-local import handling (working-directory resolution):")
    lines.append(f"  notebooks where a repo-local import failure was investigated: {len(searched)}")
    lines.append(f"    recovered by running from a different directory (now in scope): {len(recovered)}")
    lines.append(f"      of which then fixed/passing: {len(recovered_fixed)}")
    lines.append(f"    still unresolved, stay out of scope as repo-local module: {len(still_local)}")
    refused_total = sum(r.get("refused_local_module_installs") or 0 for r in discovered)
    lines.append(f"  PyPI installs refused because the repo ships its own module of that name: {refused_total}")
    lines.append("")

    # --- Repair levers (TASK_improve_fix_rate.md) ---
    # Which levers were switched on, what they did, and which fixes they produced.
    configs = sorted({r.get("levers") or "" for r in discovered})
    lever_counts: Counter = Counter()
    fixed_by_lever: dict[str, list[str]] = defaultdict(list)
    for r in discovered:
        for a in r.get("fix_actions") or []:
            if a.get("lever"):
                lever_counts[a["lever"]] += 1
        if r.get("fixed"):
            used = {a.get("lever") for a in (r.get("fix_actions") or []) if a.get("lever")}
            for lever in used:
                fixed_by_lever[lever].append(r["repository"])
    nudged = [r for r in discovered if r.get("fallback_nudges")]
    lines.append("Repair levers:")
    lines.append(f"  configuration(s) in this result set: {', '.join(c or 'none (baseline)' for c in configs)}")
    if lever_counts:
        lines.append("  lever actions taken: " + ", ".join(f"{k} x{v}" for k, v in sorted(lever_counts.items())))
    else:
        lines.append("  lever actions taken: none")
    for lever, repos in sorted(fixed_by_lever.items()):
        lines.append(f"  fixed with a {lever} step in the accepted fix: {len(repos)} ({', '.join(sorted(repos))})")
    lines.append(f"  bounded-fallback nudges given: {sum(len(r['fallback_nudges']) for r in nudged)} across {len(nudged)} notebooks")
    changed_py = [r for r in discovered if r.get("python_version_final")]
    lines.append(f"  interpreter stepped down by the agent: {len(changed_py)}")
    era_rows = [r for r in discovered if "era_resolve" in (r.get("levers") or "").split(",")]
    if era_rows:
        # lever era_resolve (TASK_era_consistent_resolution.md): where it applied and where it could not.
        setup_ok = sum(1 for r in era_rows if (r.get("era_resolution_setup") or {}).get("applied"))
        no_date = [r["repository"] for r in era_rows if (r.get("era_resolution_setup") or {}).get("reason") == "no_repo_date"]
        agent = [r.get("era_resolution_agent") or {} for r in era_rows]
        lines.append(f"  era_resolve on for {len(era_rows)} notebooks: environment build date-constrained for {setup_ok}"
                     + (f"; no repo date for {len(no_date)} ({', '.join(sorted(no_date))})" if no_date else ""))
        lines.append(f"    agent installs: {sum(a.get('installs', 0) for a in agent)} total, "
                     f"{sum(a.get('applied', 0) for a in agent)} era-constrained, "
                     f"{sum(a.get('fallback', 0) for a in agent)} fell back to plain pip (no consistent set), "
                     f"{sum(a.get('not_applied', 0) for a in agent)} not applicable (conda / no date)")
    lines.append("")

    # --- Interpreter repair provenance (TASK_interpreter_repair.md Part C) ---
    def _source_kind(row: dict) -> str:
        src = row.get("python_version_source")
        if not src or src == "none":
            return "none"
        return "era-inferred" if src == "era-inferred" else "declared"

    declared = [r for r in in_scope if _source_kind(r) == "declared"]
    era = [r for r in in_scope if _source_kind(r) == "era-inferred"]
    no_signal = [r for r in in_scope if _source_kind(r) == "none"]
    provisioned = [r for r in in_scope if r.get("python_mechanism") in ("conda", "uv", "reused")]
    substituted = [
        r for r in provisioned
        if r.get("python_version_requested") and r.get("python_version_actual")
        and r["python_version_requested"] != r["python_version_actual"]
    ]
    lines.append("Interpreter repair, among in-scope attempted notebooks:")
    lines.append(f"  version declared in the repo's own recipe (grounded): {len(declared)}")
    lines.append(f"  version era-inferred from last-commit date (ungrounded): {len(era)}")
    lines.append(f"  no signal (harness default interpreter kept): {len(no_signal)}")
    lines.append(f"  a different interpreter than the harness default was provisioned: {len(provisioned)}")
    lines.append(f"    of which the exact version was unreachable and the nearest available used: {len(substituted)}")
    fixed_provisioned = sum(1 for r in provisioned if r.get("fixed"))
    lines.append(f"    fixed after interpreter provisioning: {_pct(fixed_provisioned, len(provisioned))}")
    lines.append("")

    # --- Stop-reason breakdown (of the in-scope, not-fixed set) ---
    # TASK_agent_stopping.md: which STOP_* condition actually ended each
    # non-fix -- "step_cap" alone (the old max_steps_hit framing) conflated
    # a genuinely-still-working run with one that was obviously stuck; the
    # no-progress/give-up/model-stopped conditions tell those apart.
    not_fixed = [r for r in in_scope if not r.get("fixed")]
    stop_reason_counts = Counter(r.get("stop_reason") or "unknown" for r in not_fixed)
    stop_reason_labels = {
        "step_cap": "ran out of the step budget, not proven unfixable",
        "no_progress": "stopped early -- the observed failure stopped changing",
        "model_gave_up": "stopped early -- the model called report_unfixable",
        "model_stopped": "stopped early -- the model stopped calling tools without verify succeeding",
        "error": "stopped on an LLM/SDK error",
        "unknown": "no stop_reason recorded (result predates TASK_agent_stopping.md)",
    }
    lines.append("Stop reason, among in-scope not-fixed notebooks:")
    for reason, label in stop_reason_labels.items():
        if stop_reason_counts.get(reason):
            lines.append(f"  {reason} ({label}): {stop_reason_counts[reason]}")
    lines.append("")

    # --- Cross-file interaction (the repo-scale finding) ---
    free_passes = [r for r in in_scope if r.get("cross_file_free_pass")]
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
