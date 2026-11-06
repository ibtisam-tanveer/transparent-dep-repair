"""Lever era_date -- the notebook's TRUE era (TASK_true_era_dating.md, part A).

ERA_RESOLUTION_RESULTS.md found that the repository's LAST commit date is often years after the
notebook was written (MaaSSim: first commit 2020-09, last 2023-02), so "resolve as of that date" let
almost every modern package in. This module picks a better date per notebook and says where it came from.

Priority (each is recorded; the first two are the grounded signals):
  1. paper_date        the publication date of the article the notebook belongs to (GigaScience
                       dataset, `article.published_date`; accepted/received date when that is missing)
  2. file_git_date     the notebook file's own last change in git history (needs a FULL clone)
  3. repo_first_commit the repository's first commit
  4. repo_last_commit  the old behaviour; last resort

One sanity bound, applied to the paper date and the file date: an era date is never later than the
repository's last commit (the code cannot postdate its own last commit). Where the paper was published
after the last commit (h5n1, matrix_paper, interpretable-generative) the date is therefore capped; the row
says so (`era_date_capped_by_last_commit`). All candidate dates are kept in `era_date_candidates` so a
reader can see what any other rule would have chosen.

Several target notebooks in one repository share one environment, so the LATEST of their dates is used
(a package that existed for one of them must not be excluded). Never invents a date: with nothing available
the result is None.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

PAPER = "paper_date"
FILE = "file_git_date"
REPO_FIRST = "repo_first_commit"
REPO_LAST = "repo_last_commit"
FILE_FIRST = "file_first_commit"
PREFER_ENV = "REPAIR_TOOL_ERA_DATE_PREFER"  # "paper" (default, the task order) | "file_first" (exploratory)
GROUNDED = {PAPER, FILE, FILE_FIRST}


@dataclass
class EraDate:
    date: str | None
    source: str
    grounded: bool
    capped: bool = False
    candidates: dict = field(default_factory=dict)
    note: str = ""

    def as_meta(self) -> dict:
        return {
            "era_date": self.date, "era_date_source": self.source, "era_date_grounded": self.grounded,
            "era_date_capped_by_last_commit": self.capped, "era_date_candidates": self.candidates,
            "era_date_note": self.note,
        }


def _git(repo_path: str, *args: str) -> str:
    try:
        completed = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def is_shallow(repo_path: str) -> bool:
    return _git(repo_path, "rev-parse", "--is-shallow-repository") == "true"


def git_dates(repo_path: str, notebook_path: str | None) -> dict:
    """{repo_last_commit, repo_first_commit, file_last_change, file_first_commit, shallow} (missing = absent)."""
    out: dict = {}
    last = _git(repo_path, "log", "-1", "--format=%ad", "--date=short")
    if last:
        out["repo_last_commit"] = last
    shallow = is_shallow(repo_path)
    out["shallow"] = shallow
    if shallow:
        return out  # history was cut: first-commit / file dates would all be the single commit's date
    first = _git(repo_path, "log", "--reverse", "--format=%ad", "--date=short")
    if first:
        out["repo_first_commit"] = first.splitlines()[0]
    if notebook_path:
        file_last = _git(repo_path, "log", "-1", "--format=%ad", "--date=short", "--", notebook_path)
        if file_last:
            out["file_last_change"] = file_last
        file_first = _git(repo_path, "log", "--follow", "--diff-filter=A", "--format=%ad", "--date=short", "--", notebook_path)
        if file_first:
            out["file_first_commit"] = file_first.splitlines()[-1]
    return out


def choose_one(paper_date: str | None, paper_kind: str, git: dict, prefer: str = "paper") -> EraDate:
    """The date for ONE notebook, by the priority order, bounded by the repo's last commit."""
    last = git.get("repo_last_commit")
    cands = {"paper_date": (paper_date or None), "paper_date_kind": paper_kind if paper_date else None,
             "file_git_date": git.get("file_last_change"), "file_first_commit": git.get("file_first_commit"),
             "repo_first_commit": git.get("repo_first_commit"), "repo_last_commit": last}
    note = "shallow clone: no file/first-commit dates available" if git.get("shallow") else ""

    def bounded(date: str) -> tuple[str, bool]:
        return (last, True) if last and date > last else (date, False)

    if prefer == "file_first" and git.get("file_first_commit"):
        # exploratory ordering: when the notebook was FIRST committed (closest to when it was written)
        return EraDate(git["file_first_commit"], FILE_FIRST, True, False, cands, note)
    if paper_date:
        date, capped = bounded(paper_date[:10])
        return EraDate(date, PAPER, True, capped, cands, note)
    if git.get("file_last_change"):
        date, capped = bounded(git["file_last_change"])
        return EraDate(date, FILE, True, capped, cands, note)
    if git.get("repo_first_commit"):
        return EraDate(git["repo_first_commit"], REPO_FIRST, False, False, cands, note)
    if last:
        return EraDate(last, REPO_LAST, False, False, cands, note)
    return EraDate(None, "none", False, False, cands, note or "no date available")


def resolve(repo_path: str, notebook_paths: list[str] | None, hints: dict | None = None) -> EraDate:
    """The repository-level era date. `hints` maps notebook path -> {"paper_date", "paper_date_kind"}."""
    hints = hints or {}
    targets = list(notebook_paths or []) or [None]
    chosen = []
    for nb in targets:
        h = hints.get(nb) or {}
        chosen.append(choose_one(h.get("paper_date") or None, h.get("paper_date_kind") or "published", git_dates(repo_path, nb),
                                 prefer=os.environ.get(PREFER_ENV, "paper")))
    dated = [c for c in chosen if c.date]
    if not dated:
        return chosen[0]
    best = max(dated, key=lambda c: c.date)  # the latest era among the targets
    if len(dated) > 1:
        best.note = (best.note + "; " if best.note else "") + f"latest of {len(dated)} target notebooks"
    return best
