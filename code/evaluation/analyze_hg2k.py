"""Analysis of a run on HG2.9K (Gistable hard gists) -- TASK_hg2k_evaluation.md Parts B and C.

    python evaluation/analyze_hg2k.py evaluation/results/<tag>.jsonl [--csv evaluation/samples/hg2k_all.csv]

Reads one run's result rows (and the static columns of the prepared CSV) and prints the numbers that
HG2K_RESULTS.md reports, as markdown, plus a JSON summary next to the result file. Nothing is estimated:
every figure is a count over the rows, and a rate always comes with its denominator and an exact
(Wilson) 95% interval, because the run is a sample of the 2,891 gists.

SUCCESS DEFINITIONS (all computed, none hidden; see HG2K_RESULTS.md section 2 for why there are several)
  strict          the gist runs to exit status 0 within the time limit (the harness's own `fixed`).
  import_resolved the ImportError-resolution criterion of DockerizeMe and PyEGo: the gist no longer fails
                  with ImportError/ModuleNotFoundError. Gists that fail for some OTHER reason, or that start
                  and are still running at the time limit, count as resolved (the import line was passed).
                  One deliberate tightening: a SyntaxError means the file was never executed, so the imports
                  were never reached -- that is NOT counted as resolved (under DockerizeMe's Python 2.7 the
                  same file would have parsed; this tool does not provision Python 2, see the report).
  pllm_style      PLLM's criterion: runs "without critical runtime errors such as ImportError,
                  ModuleNotFoundError, AttributeError, or SyntaxError" -- import_resolved, and in addition
                  no AttributeError.
Each of the three is also reported "environment only" (the tool made no edit to the gist's own code): the
published baselines produce a configuration and never edit code, this tool may (edit_code).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from collections import Counter

IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}
SYNTAX_ERRORS = {"SyntaxError", "IndentationError", "TabError"}
TIMEOUT_REASONS = {"cell_timeout", "cell_timeout_download", "notebook_hard_timeout"}
CONF_RANK = {"low": 0, "medium": 1, "medium-high": 2, "high": 3}
FIX_TOOLS = {"install_package", "edit_code", "downgrade_search", "lower_python"}
_EXC_RE = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning|Interrupt))\b")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Exact-enough 95% Wilson score interval for k successes of n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def pct(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.1f}% ({k}/{n}; 95% CI {100 * lo:.1f}-{100 * hi:.1f}%)"


def exception_name(error_text: str) -> str:
    """The exception class of the last observed failure ('' when none was observed)."""
    for line in (error_text or "").splitlines():
        m = _EXC_RE.match(line)
        if m:
            return m.group(1).split(".")[-1]
    return ""


def load_rows(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_static(csv_path: str) -> dict[str, dict]:
    if not csv_path or not os.path.isfile(csv_path):
        return {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        return {r["repository"]: r for r in csv.DictReader(f)}


def not_attempted_reason(r: dict) -> str:
    """'' if the tool really worked on this gist; else why it did not (never counted as a repair failure)."""
    if not r.get("discovered", True):
        return "not_run: " + (r.get("error") or "not discovered")[:80]
    if r.get("error", "").startswith("container unavailable"):
        return "not_run: container unavailable"
    if r.get("budget_exhausted"):
        return "not_run: time budget exhausted"
    if r.get("quota_exhausted"):
        return "not_run: model quota exhausted"
    if r.get("too_large_for_context"):
        return "not_run: larger than the model context"
    # the model endpoint itself failed (network/VPN/server): the agent never got a fair attempt
    if not r.get("fixed") and r.get("stop_reason") == "error":
        return "not_run: agent error (" + (r.get("error") or "")[:60] + ")"
    return ""


def classify(r: dict, static: dict | None) -> dict:
    """Everything the report needs to know about one gist."""
    fix_actions = r.get("fix_actions") or []
    edit_used = any(a.get("tool") == "edit_code" for a in fix_actions)
    exc = "" if r.get("fixed") else exception_name(r.get("runtime_observed_error") or "")
    reason = "" if r.get("fixed") else (r.get("runtime_observed_reason") or "")
    skipped = not_attempted_reason(r)
    fixed = bool(r.get("fixed")) and not skipped
    timed_out = (not fixed) and reason in TIMEOUT_REASONS
    # A failure that is observed and is not an import problem / syntax problem means the import lines were passed.
    passed_imports = fixed or timed_out or (
        bool(exc) and exc not in IMPORT_ERRORS and exc not in SYNTAX_ERRORS and not reason.startswith("dependency:")
    )
    if exc in SYNTAX_ERRORS:
        passed_imports = False
    if skipped:
        passed_imports = False
    # an unobserved final state (no failing run seen) is not evidence of anything
    import_resolved = passed_imports
    pllm_style = import_resolved and exc != "AttributeError" and not (
        reason == "dependency:module_attribute_removed" or reason == "dependency:object_attribute_error"
    )
    if skipped:
        bucket = "not_run"
    elif fixed and r.get("passed_without_agent_action"):
        bucket = "ran_as_is"
    elif fixed:
        bucket = "fixed_with_code_edit" if edit_used else "fixed_environment_only"
    elif exc in SYNTAX_ERRORS:
        bucket = "unfixed_syntax_error"
    elif timed_out:
        bucket = "unfixed_runs_but_times_out"
    elif reason.startswith("dependency:") or exc in IMPORT_ERRORS:
        bucket = "unfixed_import_still_failing"
    elif reason == "unobserved" or not reason:
        bucket = "unfixed_unobserved"
    else:
        bucket = "unfixed_other_runtime_error"

    confs = [a.get("confidence") for a in fix_actions if a.get("confidence")]
    groundings = [a.get("grounding") for a in fix_actions]
    return {
        "repository": r["repository"], "bucket": bucket, "fixed": fixed, "skipped": skipped,
        "edit_used": edit_used, "final_exception": exc, "reason": reason,
        "strict": fixed, "import_resolved": import_resolved, "pllm_style": pllm_style,
        "py3_parses": (static or {}).get("py3_parses") == "True" if static else None,
        "llm_calls": r.get("llm_calls") or 0, "tokens": r.get("total_tokens") or 0,
        "seconds": r.get("repo_seconds"), "steps": r.get("num_agent_steps") or 0,
        "stop_reason": r.get("stop_reason") or "",
        "levers_used": sorted({a.get("lever") for a in fix_actions if a.get("lever")}),
        "tools_used": sorted({a.get("tool") for a in fix_actions}),
        "min_confidence": min(confs, key=lambda c: CONF_RANK.get(c, -1)) if confs else "",
        "groundings": groundings, "python_final": r.get("python_version_final") or r.get("python_version_actual"),
    }


def table(rows: list[tuple], header: tuple) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def analyse(rows: list[dict], static: dict[str, dict]) -> dict:
    cls = [classify(r, static.get(r["repository"])) for r in rows]
    n = len(cls)
    summary: dict = {"n_gists": n}
    attempted = [c for c in cls if not c["skipped"]]
    summary["n_attempted"] = len(attempted)
    summary["n_not_run"] = n - len(attempted)

    # --- success under each definition (denominator = every gist of the sample; a gist the tool never got to
    # --- counts against it, so the figure cannot be inflated by skipping hard cases)
    defs = {}
    for name in ("strict", "import_resolved", "pllm_style"):
        k = sum(c[name] for c in cls)
        k_env = sum(c[name] and not c["edit_used"] for c in cls)
        defs[name] = {"k": k, "k_environment_only": k_env, "n": n}
    summary["definitions"] = defs

    summary["buckets"] = dict(Counter(c["bucket"] for c in cls))
    summary["not_run_reasons"] = dict(Counter(c["skipped"] for c in cls if c["skipped"]))

    # --- Python 2 stratification (static: the file does not parse as Python 3)
    strata = {}
    for label, flag in (("python3_parses", True), ("python2_syntax", False)):
        sub = [c for c in cls if c["py3_parses"] is flag]
        strata[label] = {
            "n": len(sub),
            **{name: sum(c[name] for c in sub) for name in ("strict", "import_resolved", "pllm_style")},
            "calls": sum(c["llm_calls"] for c in sub),
        }
    summary["python2_strata"] = strata

    # --- what unfixed gists ended on
    summary["unfixed_final_exceptions"] = Counter(
        c["final_exception"] or c["reason"] or "none" for c in cls if not c["fixed"] and not c["skipped"]
    ).most_common(25)

    # --- what fixed-by-tool gists needed (provenance)
    by_tool = [c for c in cls if c["fixed"] and c["bucket"] != "ran_as_is"]
    summary["fixed_by_tool"] = {
        "n": len(by_tool),
        "min_confidence": dict(Counter(c["min_confidence"] or "none" for c in by_tool)),
        "action_groundings": dict(Counter(g for c in by_tool for g in c["groundings"])),
        "tools": dict(Counter(t for c in by_tool for t in c["tools_used"])),
        "levers": dict(Counter(l for c in by_tool for l in c["levers_used"])),
        "all_actions_metadata_grounded": sum(1 for c in by_tool if c["groundings"] and all(g == "metadata_grounded" for g in c["groundings"])),
    }

    # --- cost
    def stats(key, only=None):
        vals = [c[key] for c in (cls if only is None else only) if c[key] not in (None, "")]
        if not vals:
            return {}
        return {"total": sum(vals), "mean": round(statistics.mean(vals), 1), "median": statistics.median(vals), "max": max(vals)}

    summary["cost"] = {"llm_calls": stats("llm_calls"), "tokens": stats("tokens"), "seconds_per_gist": stats("seconds"),
                       "calls_fixed_by_tool": stats("llm_calls", by_tool),
                       "calls_unfixed": stats("llm_calls", [c for c in attempted if not c["fixed"]])}
    summary["stop_reasons"] = dict(Counter(c["stop_reason"] or "(none)" for c in attempted))
    summary["_classified"] = cls
    return summary


# ---- Part C: prior work, every number with its source. Nothing here is ours and nothing is rounded.
PRIOR = [
    # (tool, successes, of, source/definition note, verified)
    ("DockerizeMe (own paper)", 892, 2891, "Horton & Parnin, ICSE 2019, Sec. VII-B1: 'an additional 892 gists (31%) executed without experiencing "
     "Python's ImportError'; clean Python 2.7.14 image; installation failures ignored", True),
    ("DockerizeMe (PyEGo's re-run)", 888, 2891, "Ye et al., ICSE 2022, Table 4 (30.72%); Python 2.7; 888 instead of 892 because some packages were removed from PyPI", True),
    ("pipreqs (PyEGo's run)", 297, 2891, "Ye et al., ICSE 2022, Table 4 (10.27%); Python 2.7", True),
    ("PyEGo (own paper)", 1334, 2891, "Ye et al., ICSE 2022, Sec. 5.2/Table 4 (46.14%): the target program no longer experiences ImportErrors; "
     "infers Python 2.7 for 528, 3.9 for 765, 3.8 for 41 of the 1,334", True),
    ("PyEGo (PLLM's re-run)", 1302, 2891, "Bartlett et al., arXiv:2501.16191v2, Table III; default settings", True),
    ("ReadPyE (PLLM's run)", 1365, 2891, "Bartlett et al., arXiv:2501.16191v2, Table III; KG0, Python modules only; ReadPyE's OWN reported "
     "number was not accessible (TODO verify against Cheng et al., TSE 2024)", True),
    ("PLLM, 10 runs aggregated", 1583, 2891, "Bartlett et al., arXiv:2501.16191v2, Table III: Gemma2-9B with RAG; 'executed PLLM 10 times, "
     "aggregating the data'; success = runs without ImportError/ModuleNotFoundError/AttributeError/SyntaxError", True),
]
PLLM_SINGLE_RUN_APPROX = 1440  # '~1,440' (Sec. V-A) -- an approximation stated by the paper, shown as such


def comparison_table(summary: dict) -> str:
    n = summary["n_gists"]
    d = summary["definitions"]
    rows = []
    for name, k, of, note, _ in PRIOR:
        rows.append((name, f"{k}/{of}", f"{100 * k / of:.2f}%", "all 2,891", note))
    rows.append(("PLLM, single run", f"~{PLLM_SINGLE_RUN_APPROX}/2891", f"~{100 * PLLM_SINGLE_RUN_APPROX / 2891:.1f}%", "all 2,891",
                 "Bartlett et al., Sec. V-A: 'A single run achieves ~1,440 fixes' (approximate, as stated)"))
    for label, key in (("**This tool**, import-resolved criterion (DockerizeMe/PyEGo-style)", "import_resolved"),
                       ("**This tool**, PLLM-style criterion", "pllm_style"),
                       ("**This tool**, strict (runs to exit 0)", "strict")):
        k = d[key]["k"]
        lo, hi = wilson(k, n)
        rows.append((label, f"{k}/{n}", f"{100 * k / n:.1f}% (CI {100 * lo:.1f}-{100 * hi:.1f}%)", f"random sample of {n}", "this run"))
    return table(rows, ("Tool", "Fixed", "Rate", "Gists", "Source / criterion"))


def render(summary: dict) -> str:
    n = summary["n_gists"]
    out = [f"# HG2.9K analysis ({n} gists)", ""]
    out.append(f"attempted by the tool: {summary['n_attempted']}; not run: {summary['n_not_run']} {summary['not_run_reasons']}")
    out += ["", "## Success under each definition (denominator = all gists of the sample)", ""]
    d = summary["definitions"]
    out.append(table([
        ("strict (exit 0)", pct(d["strict"]["k"], n), d["strict"]["k_environment_only"]),
        ("import-resolved (DockerizeMe/PyEGo-style)", pct(d["import_resolved"]["k"], n), d["import_resolved"]["k_environment_only"]),
        ("PLLM-style", pct(d["pllm_style"]["k"], n), d["pllm_style"]["k_environment_only"]),
    ], ("criterion", "rate", "of which with NO edit to the gist's code")))
    out += ["", "## Outcome buckets", ""]
    order = ["ran_as_is", "fixed_environment_only", "fixed_with_code_edit", "unfixed_import_still_failing", "unfixed_syntax_error",
             "unfixed_runs_but_times_out", "unfixed_other_runtime_error", "unfixed_unobserved", "not_run"]
    out.append(table([(b, summary["buckets"].get(b, 0), f"{100 * summary['buckets'].get(b, 0) / n:.1f}%") for b in order],
                     ("bucket", "gists", "share")))
    out += ["", "## Python 2 (static: the file does not parse as Python 3)", ""]
    s = summary["python2_strata"]
    out.append(table([
        (label, v["n"], pct(v["strict"], v["n"]), pct(v["import_resolved"], v["n"]), pct(v["pllm_style"], v["n"]), v["calls"])
        for label, v in s.items()
    ], ("stratum", "gists", "strict", "import-resolved", "PLLM-style", "LLM calls")))
    out += ["", "## What the unfixed gists ended on", ""]
    out.append(table(summary["unfixed_final_exceptions"], ("final exception / reason", "gists")))
    f = summary["fixed_by_tool"]
    out += ["", f"## Provenance of the {f['n']} gists the tool fixed (excluding 'ran as is')", "",
            f"lowest confidence over each gist's fix actions: {f['min_confidence']}",
            f"grounding of the individual fix actions: {f['action_groundings']}",
            f"gists whose every action was registry-grounded: {f['all_actions_metadata_grounded']}",
            f"tools used: {f['tools']}; levers: {f['levers']}"]
    c = summary["cost"]
    out += ["", "## Cost", "", f"LLM calls: {c['llm_calls']}", f"tokens: {c['tokens']}", f"seconds per gist (container+repair): {c['seconds_per_gist']}",
            f"calls per gist the tool fixed: {c['calls_fixed_by_tool']}", f"calls per gist left unfixed: {c['calls_unfixed']}",
            f"stop reasons: {summary['stop_reasons']}"]
    out += ["", "## Comparison with prior work", "", comparison_table(summary)]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+", help="one or more result jsonl files of the same run (e.g. the resumed parts)")
    parser.add_argument("--csv", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "hg2k_all.csv"))
    args = parser.parse_args(argv)
    rows = []
    for path in args.results:
        rows += load_rows(path)
    # one row per gist (a resumed run can in principle repeat one): keep the last
    rows = list({r["repository"]: r for r in rows}.values())
    summary = analyse(rows, load_static(args.csv))
    print(render(summary))
    out_path = args.results[0].replace(".jsonl", ".hg2k_summary.json")
    slim = {k: v for k, v in summary.items() if k != "_classified"}
    slim["gists"] = [{k: v for k, v in c.items() if k not in ("groundings",)} for c in summary["_classified"]]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(slim, f, indent=2, default=str)
    print(f"\n(summary written to {out_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
