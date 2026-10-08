# Dependency-Failure Taxonomy

A classification of *why* a scientific notebook or script fails to run again, by the
**cause** of the failure — not by the surface symptom. This is the reference the
evaluation uses to label the GigaScience failure set and to report repair success
*per category*. It is also a thesis artifact in its own right: it is the justified
scheme behind the Results chapter.

Give this document to the agent alongside `TASK_evaluation.md`; it is "the taxonomy
draft / the taxonomy doc" that task refers to.

---

## 1. Why classify by cause, not symptom

The same Python error message can have completely different causes, and the same
cause can surface as different error messages. For example:

- An `ImportError` can mean *the package isn't installed* (cause: missing dependency)
  **or** *the package is installed but the name moved in a newer version* (cause:
  moved/renamed import). Same symptom, two different causes, two different fixes.
- An `AttributeError` can mean *an API was removed in a newer version of a library*
  (a dependency cause) **or** *an ordinary bug in the author's own logic* (not a
  dependency problem at all). Same symptom, one is in scope, one is not.

If results are reported by symptom ("we fixed X% of ImportErrors"), they hide the
interesting story and aren't defensible. Reporting by **cause** — "the tool fixes
missing dependencies near-perfectly, is moderate on moved imports, and weak on
removed APIs" — is the honest, useful finding. So every failure is assigned to one
cause category, using a decision rule, with a confidence tier.

---

## 2. The categories (A–E)

Each failing notebook is assigned **exactly one** category — its primary cause.

### A — Missing dependency
A package the code needs is simply not installed in the environment.
- **Typical signal:** `ModuleNotFoundError` / `ImportError` for a top-level package
  name that exists on PyPI and is not in the environment.
- **Fix shape:** install the package (ideally at a compatible version).
- **Expectation:** the tool's strongest category — this is the grounded,
  metadata-backed case.

### B — Moved / renamed import
The package *is* available, but the import path changed across versions (a symbol or
submodule moved, was renamed, or was reorganised).
- **Typical signal:** `ImportError: cannot import name X from Y`, or an import of a
  submodule path that no longer exists, where the package itself is present.
- **Fix shape:** adapt the import to the new location/name (a code change), or pin to
  a version where the old path still exists.
- **Expectation:** mixed — some are metadata/known-mapping cases, many are
  model-proposed code edits.

### C — Removed / changed API
The package is present and imported fine, but a function, method, attribute, or
argument the code uses was **removed or changed** in the installed version.
- **Typical signal:** `AttributeError` (e.g. `np.float` removed in numpy ≥ 1.24),
  `TypeError` on a changed signature, deprecation-turned-removal.
- **Fix shape:** either update the code to the new API, or pin to an older
  compatible version of the package.
- **In scope but as a *candidate*** by default (see §3): an `AttributeError` is only
  a dependency failure if the cause is a version change in a third-party package, not
  a bug in the author's own code. Confirm before counting it as confirmed.
- **Expectation:** the hard category — honest if the tool is weaker here.

### D — Incompatible / version conflict
The individual packages install, but their versions are mutually incompatible, or a
package is incompatible with the Python version — the environment cannot be satisfied
as specified.
- **Typical signal:** resolver/pip conflict errors, `VersionConflict`, a package that
  won't install against the target Python, an import that fails because a *transitive*
  dependency is at an incompatible version.
- **Fix shape:** find a mutually compatible set of versions (constraint solving /
  pinning).
- **Expectation:** partially grounded (resolver/metadata), partially search.

### E — Other / not a dependency failure
The notebook fails for a reason that is **not** a dependency-configuration problem,
so it is **out of scope** for this tool and must not be counted as a repair failure.
- **Examples:** missing data files, missing credentials/API keys, needs a GPU or a
  network service, genuine bugs in the author's own code, notebooks that run for
  hours or require manual input, non-Python errors.
- **Fix shape:** none (out of scope).
- **Handling:** recorded as **excluded** from the repair denominator — reported
  separately as "not in scope", never as a failure of the tool.

---

## 3. Confidence tiers (confirmed / candidate / excluded)

Classification from a dataset alone is imperfect — you often have the error type but
not certainty about the underlying cause. So each labelled failure also carries a
tier:

- **confirmed** — the evidence clearly supports this cause (e.g. a
  `ModuleNotFoundError` for a package absent from the environment → confirmed A).
- **candidate** — the symptom is consistent with this cause but could be something
  else, and it hasn't been verified (e.g. any `AttributeError` → candidate C until
  checked; it might be the author's own bug = E).
- **excluded** — determined to be out of scope (category E), or not a genuine
  dependency failure at all.

Report results two ways: on **confirmed** cases (the clean denominator) and on
**confirmed + candidate** (the generous denominator). The gap between them is itself
honest information about label uncertainty.

---

## 4. How the categories map to what the tool already detects

The repair engine's `diagnose.py` already classifies errors into its own small set of
"kinds". The taxonomy sits *above* that — it's the cause-level grouping the
evaluation reports on. The mapping (fill in exactly against the current
`diagnose.py`, this is the expected shape):

| diagnose.py kind (symptom)              | usually maps to taxonomy cause | default tier |
|-----------------------------------------|--------------------------------|--------------|
| module not found                        | A (missing dependency)         | confirmed    |
| cannot import name / bad import path    | B (moved/renamed import)       | candidate    |
| attribute/API error                     | C (removed/changed API)        | candidate    |
| version/resolver conflict               | D (version conflict)           | confirmed    |
| other / unrecognised                    | E (out of scope) or re-examine | excluded     |

This mapping is a *starting heuristic for labelling*, not a hard rule — the tiers
exist precisely because the symptom doesn't always fix the cause.

---

## 5. How to apply it to the GigaScience failure set (step 1.1 of the eval task)

1. From the 2023 `db.sqlite`, pull the failing executions and their error type /
   reason (reuse the authors' own `get_repro_missing_dependencies()` approach, which
   keys off `ImportError`/`ModuleNotFoundError`, as the starting filter — then widen).
2. Assign each failing notebook a **category (A–E)** and a **tier** using §2–§4.
3. Write `dataset/failures_2023.csv`: one row per notebook — repository, notebook
   path, raw error type/reason, assigned category, tier.
4. Report the **per-category counts** and the confirmed/candidate/excluded split.
   These counts are the first result in the evaluation and define the denominators
   for per-category success rates.

---

## 6. Relationship to prior work

This scheme is compatible with, and refines, the GigaScience study's own treatment of
missing-dependency errors (their `get_repro_missing_dependencies()` captured category
A). It extends beyond "missing vs. not" to separate the *kinds* of dependency decay
(A–D) and to fence off non-dependency failures (E) honestly — which is what lets the
thesis report differentiated, defensible repair results rather than a single blended
number. Where the error-type breakdown in the GigaScience paper (their results tables)
lines up with these categories, cite that alignment in the thesis.

---

## 7. Known limits (state these honestly in the thesis)

- Labelling from stored error text is imperfect; the candidate tier exists for this
  reason, and results are reported on both denominators.
- A single notebook can fail for more than one reason; it is assigned its *primary*
  (first-encountered) cause, and multi-cause cases are noted where they occur.
- Category C vs. E (removed API vs. author's own bug) is the hardest line to draw and
  is where most candidate labels will sit — don't over-claim confirmed Cs.
