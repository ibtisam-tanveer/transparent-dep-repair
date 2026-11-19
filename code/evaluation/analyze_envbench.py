"""Analysis of an EnvBench (Python subset) run -- TASK_envbench_evaluation.md sections 4, 5 and 8.

    python evaluation/analyze_envbench.py evaluation/results/envbench_py_main.jsonl [more result files of the SAME run]
        [--csv evaluation/samples/envbench_py_all.csv]

Several result files (a resumed run, a retry file) are merged one row per repository, the LAST file winning.
Writes `<first results file>.envbench_summary.json` and prints the markdown that goes into ENVBENCH_RESULTS.md.

Success definitions (the denominator is always the whole list of 329 repositories, so a repository the tool never got
to counts against it):

* PASS (EnvBench-metric, primary): EnvBench's own Python rule -- pyright, `--level error`, zero `reportMissingImports`
  diagnostics -- evaluated in the environment the tool built (evaluation/envbench_check.py). This is the number that
  goes next to the paper's Table 1. In EnvBench it is "zero exit code and zero reported issues"; here the exit code is
  that of the check, because the environment is scored in place, not by replaying a setup script (see the results
  file, section "How a repository becomes a target").
* import-resolved: for this dataset identical to PASS by construction (the only observable is "no missing import
  left"), so it is reported once, not as a second number.
* strict (the script runs to exit 0): not applicable -- most library repositories have no runnable target.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.analyze_hg2k import CONF_RANK, exception_name, load_rows, not_attempted_reason, pct, wilson  # noqa: E402

_ISSUES_RE = re.compile(r"\[EnvBench check: (\d+) missing-import diagnostics, (\d+) modules\]")
_MODULE_RE = re.compile(r"No module named '([^']+)'")

# Table 1 of the EnvBench paper (Eliseeva et al., arXiv:2503.14443v1), Python columns: pass@1 and the count of the 329
# repositories. Percentages as printed in the paper; counts as printed next to them. Checked 2026-11-18.
PAPER = "Eliseeva et al., arXiv:2503.14443v1, Table 1"
LEADERBOARD = [
    # (approach, passed, of, percent as printed, source)
    ("Zero-shot LLM, GPT-4o-mini", 15, 329, 4.56, PAPER),
    ("Installamatic Agent, GPT-4o-mini", 9, 329, 2.74, PAPER),
    ("Installamatic Agent, GPT-4o", 16, 329, 4.86, PAPER),
    ("Zero-shot LLM, GPT-4o", 18, 329, 5.47, PAPER),
    ("Bash Agent, GPT-4o-mini", 18, 329, 5.47, PAPER),
    ("Bash Agent, GPT-4o (best reported)", 22, 329, 6.69, PAPER),
]
BASELINE_SCRIPT = ("EnvBench's deterministic baseline script sets up 62 of the Python repositories (15.9%) that reached it; "
                   "the 329 are the ones it could not set up (Eliseeva et al., Sec. 3.3)")


def issues_in(error_text: str) -> tuple[int | None, int | None]:
    """(diagnostics, distinct modules) from the final line of the check's output, (None, None) if absent."""
    match = _ISSUES_RE.search(error_text or "")
    return (int(match.group(1)), int(match.group(2))) if match else (None, None)


def classify(r: dict, static: dict | None = None) -> dict:
    """Everything the report needs to know about one repository. `static` is unused (kept so the signature mirrors
    analyze_hg2k.classify)."""
    fix_actions = r.get("fix_actions") or []
    error = r.get("error") or ""
    final = "" if r.get("fixed") else (r.get("runtime_observed_error") or "")
    reason = "" if r.get("fixed") else (r.get("runtime_observed_reason") or "")
    skipped = not_attempted_reason(r)
    fixed = bool(r.get("fixed")) and not skipped

    worker_timeout = (not r.get("discovered", True)) and error.startswith("timed out after")
    if worker_timeout:
        skipped = ""  # the tool did run, and did not finish within the per-repository cap: a failure, not a no-show

    if skipped:
        bucket = "not_run"
    elif worker_timeout:
        bucket = "unfixed_worker_timeout"
    elif fixed and r.get("passed_without_agent_action"):
        bucket = "passed_as_is"
    elif fixed:
        bucket = "fixed_by_tool"
    elif final.startswith("EnvBenchCheckError"):
        bucket = "unfixed_check_could_not_run"
    elif reason == "cell_timeout" or final.startswith("TimeoutExpired"):
        bucket = "unfixed_check_timed_out"
    elif final.startswith("ImportError: only repository-local"):
        bucket = "unfixed_only_local_or_generated"
    elif "[EnvBench check:" in final:
        bucket = "unfixed_missing_imports_remain"
    else:
        bucket = "unfixed_other"

    initial, _ = issues_in(r.get("initial_error") or "")
    last, _ = issues_in(final)
    if fixed:
        last = 0
        if r.get("passed_without_agent_action"):
            initial = 0
    module = _MODULE_RE.search(final)
    confs = [a.get("confidence") for a in fix_actions if a.get("confidence")]
    return {
        "repository": r["repository"], "bucket": bucket, "pass": bucket in ("passed_as_is", "fixed_by_tool"),
        "skipped": skipped, "final_exception": exception_name(final), "final_module": module.group(1) if module else "",
        "issues_initial": initial, "issues_final": last, "edit_used": any(a.get("tool") == "edit_code" for a in fix_actions),
        "llm_calls": r.get("llm_calls") or 0, "tokens": r.get("total_tokens") or 0, "seconds": r.get("repo_seconds"),
        "steps": r.get("num_agent_steps") or 0, "stop_reason": r.get("stop_reason") or "",
        "tools_used": sorted({a.get("tool") for a in fix_actions}),
        "levers_used": sorted({a.get("lever") for a in fix_actions if a.get("lever")}),
        "min_confidence": min(confs, key=lambda c: CONF_RANK.get(c, -1)) if confs else "",
        "groundings": [a.get("grounding") for a in fix_actions],
        "python": r.get("python_version_actual") or "", "python_source": r.get("python_version_source") or "",
        "dependency_file": r.get("dependency_file_used") or "", "env_setup_ok": r.get("env_setup_ok"),
        "fix_packages": [a.get("resolved_package") for a in fix_actions if a.get("tool") == "install_package"],
    }


def _stats(values: list) -> dict:
    values = [v for v in values if v not in (None, "")]
    if not values:
        return {}
    return {"total": round(sum(values), 1), "mean": round(statistics.mean(values), 1), "median": statistics.median(values),
            "max": max(values)}


def analyse(rows: list[dict], static: dict | None = None) -> dict:
    cls = [classify(r, (static or {}).get(r["repository"])) for r in rows]
    n = len(cls)
    attempted = [c for c in cls if not c["skipped"]]
    passed = [c for c in cls if c["pass"]]
    k = len(passed)
    lo, hi = wilson(k, n)
    summary: dict = {
        "n_repositories": n, "n_attempted": len(attempted), "n_not_run": n - len(attempted),
        "pass": {"k": k, "n": n, "percent": round(100 * k / n, 2) if n else 0.0, "wilson_95": [round(100 * lo, 1), round(100 * hi, 1)],
                 "of_attempted": {"k": k, "n": len(attempted)}},
        "buckets": dict(Counter(c["bucket"] for c in cls)),
        "not_run_reasons": dict(Counter(c["skipped"] for c in cls if c["skipped"])),
    }
    unfixed = [c for c in attempted if not c["pass"]]
    summary["final_exceptions"] = Counter(c["final_exception"] or c["bucket"] for c in unfixed).most_common(10)
    summary["final_modules"] = Counter(c["final_module"] for c in unfixed if c["final_module"]).most_common(25)

    # progress even where the check did not pass: the missing-import diagnostics before and after the tool
    both = [c for c in attempted if c["issues_initial"] is not None and c["issues_final"] is not None]
    reduced = [c for c in both if c["issues_final"] < c["issues_initial"]]
    summary["issue_progress"] = {
        "n_with_both_counts": len(both), "n_reduced": len(reduced),
        "median_initial": statistics.median([c["issues_initial"] for c in both]) if both else None,
        "median_final": statistics.median([c["issues_final"] for c in both]) if both else None,
        "n_unfixed_reduced": sum(1 for c in reduced if not c["pass"]),
    }

    # strata: the interpreter the tool ended on, and the declared-dependency file it started from
    def stratum(key):
        out = {}
        for value in sorted({c[key] or "(none)" for c in cls}):
            sub = [c for c in cls if (c[key] or "(none)") == value]
            out[value] = {"n": len(sub), "pass": sum(c["pass"] for c in sub)}
        return out

    summary["by_python"] = stratum("python")
    summary["by_dependency_file"] = stratum("dependency_file")

    by_tool = [c for c in cls if c["bucket"] == "fixed_by_tool"]
    summary["fixed_by_tool"] = {
        "n": len(by_tool),
        "min_confidence": dict(Counter(c["min_confidence"] or "none" for c in by_tool)),
        "action_groundings": dict(Counter(g for c in by_tool for g in c["groundings"])),
        "tools": dict(Counter(t for c in by_tool for t in c["tools_used"])),
        "levers": dict(Counter(lv for c in by_tool for lv in c["levers_used"])),
        "all_actions_metadata_grounded": sum(1 for c in by_tool if c["groundings"] and all(g == "metadata_grounded" for g in c["groundings"])),
        "code_edits": sum(c["edit_used"] for c in by_tool),
    }
    summary["cost"] = {
        "llm_calls": _stats([c["llm_calls"] for c in cls]), "tokens": _stats([c["tokens"] for c in cls]),
        "seconds_per_repository": _stats([c["seconds"] for c in cls]),
        "calls_fixed_by_tool": _stats([c["llm_calls"] for c in by_tool]),
        "calls_unfixed": _stats([c["llm_calls"] for c in unfixed]),
    }
    summary["stop_reasons"] = dict(Counter(c["stop_reason"] or "(none)" for c in attempted))
    summary["_classified"] = cls
    return summary


def table(rows: list[tuple], header: tuple) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def comparison_table(summary: dict) -> str:
    p = summary["pass"]
    rows = [(name, f"{k}/{of}", f"{pc:.2f} %", src) for name, k, of, pc, src in LEADERBOARD]
    rows.append(("**This tool** (six levers, Qwen3.6-35B-A3B), EnvBench-metric", f"{p['k']}/{p['n']}", f"**{p['percent']:.2f} %**",
                 "this run, one run, in-place scoring (see caveats)"))
    return table(rows, ("Approach", "Passed", "pass@1", "Source"))


def render(summary: dict) -> str:
    n, p = summary["n_repositories"], summary["pass"]
    out = [f"# EnvBench (Python) analysis ({n} repositories)", "",
           f"attempted by the tool: {summary['n_attempted']}; not run: {summary['n_not_run']} {summary['not_run_reasons']}", "",
           "## Success (EnvBench-metric: zero reportMissingImports; denominator = the whole list)", "",
           f"PASS: **{p['percent']:.2f} %** ({p['k']}/{p['n']}); among the {p['of_attempted']['n']} attempted: {pct(p['of_attempted']['k'], p['of_attempted']['n'])}",
           f"(for a full list this is a plain proportion; 95 % Wilson interval for reference: {p['wilson_95'][0]}-{p['wilson_95'][1]} %)", "",
           "## Outcome buckets", "", table([(b, c, f"{100 * c / n:.1f} %") for b, c in sorted(summary['buckets'].items(), key=lambda kv: -kv[1])],
                                           ("bucket", "repositories", "share")), "",
           "## Progress on the missing-import count", ""]
    ip = summary["issue_progress"]
    out += [f"repositories with both counts: {ip['n_with_both_counts']}; count reduced by the tool: {ip['n_reduced']} "
            f"(of which still not passing: {ip['n_unfixed_reduced']}); median before {ip['median_initial']}, after {ip['median_final']}", "",
            "## What the unfixed repositories ended on", "", table(summary["final_exceptions"], ("final exception / bucket", "repositories")), "",
            "Most frequent module still unresolved (the one the check names first):", "",
            table(summary["final_modules"], ("module", "repositories")), "",
            "## Interpreter the tool ended on", "", table([(k, v["n"], v["pass"]) for k, v in summary["by_python"].items()], ("python", "repositories", "pass")), "",
            "## Declared dependency file the setup started from", "",
            table([(k, v["n"], v["pass"]) for k, v in summary["by_dependency_file"].items()], ("file", "repositories", "pass")), "",
            f"## Provenance of the {summary['fixed_by_tool']['n']} repositories the tool fixed ('passed as is' excluded)", ""]
    f = summary["fixed_by_tool"]
    out += [f"lowest confidence over each repository's fix actions: {f['min_confidence']}",
            f"grounding of the individual fix actions: {f['action_groundings']}",
            f"repositories whose every action was registry-grounded: {f['all_actions_metadata_grounded']}",
            f"tools used: {f['tools']}; levers: {f['levers']}; code edits: {f['code_edits']}", "", "## Cost", ""]
    c = summary["cost"]
    out += [f"LLM calls: {c['llm_calls']}", f"tokens: {c['tokens']}", f"seconds per repository: {c['seconds_per_repository']}",
            f"calls per repository the tool fixed: {c['calls_fixed_by_tool']}", f"calls per repository left unfixed: {c['calls_unfixed']}",
            f"stop reasons: {summary['stop_reasons']}", "", "## Comparison with the EnvBench paper (Table 1, Python)", "",
            comparison_table(summary), "", BASELINE_SCRIPT]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+")
    parser.add_argument("--csv", default="")
    args = parser.parse_args(argv)
    rows: list[dict] = []
    for path in args.results:
        rows += load_rows(path)
    rows = list({r["repository"]: r for r in rows}.values())
    summary = analyse(rows)
    print(render(summary))
    out_path = args.results[0].replace(".jsonl", ".envbench_summary.json")
    slim = {k: v for k, v in summary.items() if k != "_classified"}
    slim["repositories"] = [{k: v for k, v in c.items() if k != "groundings"} for c in summary["_classified"]]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(slim, f, indent=2, default=str)
    print(f"\n(summary written to {out_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
