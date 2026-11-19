# Chapter 3 (State of the Art) — status note (not committed)

Written to `thesis/content/ch03_related_work.tex` (the existing chapter file; the task's `thesis/chapters/03_state_of_the_art.tex` does not exist in this repository). New abbreviation `RAG`; new bib entries `horton2018gistable`, `pipreqs`, `yang2025kgcompass`, `mengistu2026terrarepair`. The thesis compiles with tectonic (BibTeX clean). Chapter 3 is about 7 pages (PDF pages 23-29) with one tool table.

## What the chapter now says
1. Reproducibility studies (Pimentel; Samuel & Mietchen; Jin) motivate repair; they measure decay but do not repair.
2. Knowledge-graph/rule family: pipreqs, DockerizeMe, PyEGo, PyCRE, ReadPyE, SnifferDog (+ PCREQ), each with input, output, decision basis, limits.
3. LLM/agentic family: PLLM (single file; Section VII names hybrid LLM + knowledge graph as future work), Repo2Run and EnvBench (repository scale, success measured differently), KGCompass/TerraRepair as neighbouring analogies, code-level repair of notebooks.
4. Benchmarks: Gistable/HG2.9K, GigaScience corpus, others.
5. Gap: repository scale, interpreter+packages+code, verification by execution, per-step transparency; contribution is transparency and scope, not a record rate; HG2.9K "mid-table"; mapped to RQ1-RQ3.

## Where the brief was NOT followed, because the papers say otherwise
- **KGCompass** is software *issue/bug* repair on SWE-bench Lite, not dependency repair, and not "the PyEGo/ReadPyE idea at repository scale". It is described as a neighbouring KG+LLM paper.
- **TerraRepair** repairs Terraform (infrastructure-as-code) misconfigurations, not Python dependencies. Used only as an analogy; the three claims in the brief do appear in its paper (the ablation is narrow: TerraGoat AWS, Checkov, n=158).
- **"Most classical tools do not verify by running"**: not supported. SnifferDog installs candidates and checks execution (and reproduction of stored results); ReadPyE (abstract) iterates on validation logs. DockerizeMe infers one static configuration. The chapter says this and the gap no longer relies on that claim.
- **pipreqs "no versions"**: its README shows pinning (and a `--mode` option); the chapter says that.
- **"DockerizeMe built the Gistable dataset"**: Gistable is the same authors' earlier paper (ICSME 2018); DockerizeMe defines HG2.9K as its hard subset.
- **Repo2Run's "first LLM agent"** is the authors' own claim; stated as such. Its success means the tests can run, not pass. **EnvBench's** Python success is a static missing-imports check (pyright), not execution.

## Corrections found in the supplied files
- `KEY_NUMBERS_for_thesis.md`, 2021 run: 4,169 notebooks declared dependencies, but only **2,684** had all of them installed and were re-run (396 ran without error, 245 identical) per arXiv:2209.04308. Not used in the thesis.
- `HG2K_RESULTS.md` §5: my earlier "PLLM validates against 2.7 to 3.7" rested on the PLLM threats-to-validity section. Replaced by what Sections III-G/IV-C support.

## Every `TODO verify` that remains (chapter + new bib notes)
Chapter 3:
1. pipreqs: that no academic paper exists (not searched for).
2. ReadPyE: description rests on the abstract (third-party copy) and PLLM's description; journal paper unreadable. Its own HG2.9K figure is not used; only "as measured by" PLLM.
3. PCREQ: venue (TOSEM per abstract page vs "JACM" in the HTML header) and what its "repair report" contains.
4. Statement that the classical papers do not report per-choice evidence (based on their output formats; not every section read).
5. PLLM: venue (none stated in the paper); whether it ever edits source code.
6. Repo2Run and EnvBench: venues (neither arXiv version read states one; the bib keeps "NeurIPS 2025" / "ICLR 2025" as unverified).
7. KGCompass: venue. TerraRepair: ESEM 2026 acceptance (arXiv comment only; event fields "TBD").
8. Zhu et al. (ASE 2021): everything beyond title/authors/venue/pages (IEEE page empty); tool name "Relancer" unconfirmed. Jin et al. cited only at title level.
9. Gistable: venue/pages/DOI (venue taken from DockerizeMe's reference list).
10. Gap item 4: soften if PCREQ's report records per-change evidence.
Bibliography (existing flags kept): Pimentel numbers vs the IEEE version; PLLM workshop claim; Repo2Run "spotlight"; EnvBench venue; PCREQ venue; Ryan et al. full title/volume; CopilotLens workshop; pipreqs year/maintainers.

## Described only vaguely because it could not be confirmed
ReadPyE (abstract only), Relancer/Zhu et al. (title only), PCREQ's report, PLLM's code-editing behaviour, and the classical tools' evidence reporting.

## Not done yet
The HG2.9K comparison table for the thesis (Chapter 6; the table already exists in `code/HG2K_RESULTS.md` §8 with verified numbers) is the next item after your review. Chapter 2 is not started.
