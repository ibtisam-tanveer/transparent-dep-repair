# Pilot Decisions — read before running the real pilot

The harness is validated (see `EVALUATION_PILOT_SUMMARY.md`). Before running the real
~20–50 repo pilot, apply these four decisions. They turn the smoke test's
non-meaningful 6% into a number that is honest and fair to report in the thesis.
None of them change the repair engine's logic.

## 1. Use the stronger model (not gpt-4o-mini)

The smoke test ran on `gpt-4o-mini`, which is weak and whose 128K context window
already failed on real notebooks. The supervisor has provided an API key for a more
capable model — use it for the pilot. If the key's model is not yet confirmed, ask
the user before running. Record which model was used alongside the results (it's part
of reproducibility). The provenance/transparency output does not change with the
model; only effectiveness does.

## 2. Handle the "notebook too large for context" case cleanly

Two smoke-test notebooks failed with `context_length_exceeded` — the notebook's own
code was larger than the model's context window. Currently this surfaces as an
unhelpful SDK error. Detect this case and record it as its own distinct outcome
("too large for model context"), **not** as a repair failure. A larger-context model
(step 1) will also reduce how often this happens. Optionally, trim excessively large
cell content before sending to the model — but recording it distinctly is the minimum.

## 3. Sample the pilot from CONFIRMED IN-SCOPE failures only

The smoke test sampled repos somewhat randomly. For the real pilot, draw the
evaluation set from `dataset/failures_2023.csv` restricted to **categories A, C, D**
at the **confirmed** tier (the dependency-caused failures the tool is actually meant
to fix). Exclude category E (missing data files, GPU/credentials, author bugs — not
dependency problems) from the sampled set. Category B is unobservable from the static
labels (see the taxonomy doc / `NOTES_2023.md`), so it won't appear here; that's
expected and already documented.

## 4. Report effectiveness on the IN-SCOPE denominator

The headline number must be: *of the in-scope dependency failures the tool attempted,
what fraction did it fix*. Specifically, exclude from the denominator:
- notebooks never attempted because a repo's time budget ran out (report separately);
- notebooks that failed for out-of-scope reasons discovered at run time (missing data,
  GPU, credentials) — reclassify these as category E, don't count them as failures;
- the "too large for context" cases from step 2 (report separately).

Report the number both ways where it helps: strict (confirmed only) and generous
(confirmed + candidate). Keep the per-category breakdown (A vs C vs D), the
grounded-vs-model-proposed split, the confidence distribution, and the cross-file
"fixed for free" count — exactly as the harness already produces them.

## Then: run the pilot

- Use the real per-file time budget (`--per-file-budget-minutes 10`), not the smoke
  test's tight 1.5.
- `--sample 30` (or 20–50) drawn from the in-scope set per step 3.
- It may take a few hours; the checkpoint lets it be split across sessions. Confirm
  with the user before launching a long run, and report observed cost/time.
- Write the real `EVALUATION_PILOT_SUMMARY.md` with the in-scope numbers.

## Unchanged / out of scope

No new agents, no knowledge-graph tool, no UI, no change to the repair engine's
repair logic. Steps 1–2 are reliability/measurement changes (model choice, honest
handling of an error case), not new repair behaviour.
