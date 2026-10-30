# Classical Tools & Novelty — Research Findings (draft)

**Status: DRAFT / working research notes.** Prepared in response to the supervisor's
request to research the classical (non-LLM) dependency tools and identify the
thesis's novel contribution. Sources listed at the end. Numbers and claims are from
those sources — verify each before citing in the thesis.

## 1. The one-line headline

The state-of-the-art LLM paper on this exact problem (PLLM, "The Last Dependency
Crusade" — your reference [1]) **explicitly names your proposed direction as its own
future work.** Its authors conclude that knowledge-based (classical) and prompt-based
(LLM) methods have *complementary* strengths — the classical baselines still fix a
subset PLLM fails on (legacy deps, renamed packages, system nuances) — and they
flag hybrid architectures that combine LLM reasoning with curated symbolic /
knowledge-graph knowledge as unexplored future work. So your "hybrid classical + LLM"
idea isn't a guess — it's the gap the leading paper in your field says is open. That
is the strongest possible justification for a novel contribution.

## 2. The classical (pre-LLM) landscape

All the major classical tools for Python dependency/environment inference are
**knowledge-graph (KG) based**, in one lineage:

- **DockerizeMe** (Horton & Parnin, 2019) — the origin. Builds an offline knowledge
  base from PyPI and uses graph-based inference to produce a Dockerfile for a code
  snippet. Established the **Gistable / HG2.9K** benchmark (~2,891 gists) that later
  tools all use. Motivating finding: run in a clean environment, over half of public
  Python gists fail on an import error.
- **PyEGo** (Ye et al., ICSE 2022) — your reference [2]. Neo4j knowledge graph
  ("PyKG"); infers compatible versions of third-party packages, the interpreter, and
  system libraries by solving constraints on a program-specific sub-graph. Improves
  on DockerizeMe with package docs / release metadata.
- **PyCRE** — conflict-aware inference via a domain knowledge graph; reduces version
  mismatches.
- **ReadPyE** (Cheng et al., 2024) — combines naming-similarity measures with
  optimization; iteratively validates choices against logs. A refinement of the KG
  approach (~256k nodes, ~1.9M relationships in the associated graph).
- **V2** (Horton & Parnin) — a different angle: detecting/mitigating *configuration
  drift* over time via feedback-directed search and version matrices.
- **SnifferDog** — **restores execution environments for Jupyter notebooks** by
  analysing code and mapping to compatible versions. Directly relevant to your
  notebook focus; limited on API coverage and advanced notebook features.
- **PyDFix** (Mukherjee et al., 2021) — non-KG classical: regex-based build-log
  analysis to resolve dependency errors.
- **PCREQ** (2025) — recent and important: infers *compatible requirements* for
  library upgrades. Reports a 94% inference success rate on its own benchmark
  (**REQBench**, 2,095 upgrade cases), **beating PyEGo (~37%), ReadPyE (~37%), and
  LLM approaches (GPT-4o, DeepSeek) by ~18–20%**, at ~60s/case. Shows classical
  methods are still competitive and fast.

**Shared strength of the classical tools:** fast, deterministic, no per-fix model
cost, and good on structured cases (legacy/renamed packages, system-level nuances).
**Shared weakness (stated by their own successors):** they depend on a large,
**maintained** knowledge graph that must be updated to stay current, they suffer from
incomplete domain knowledge, and they struggle with finer-grained API-level
differences.

## 3. The LLM & agentic landscape

- **PLLM** ("The Last Dependency Crusade", 2025) — reference [1]. Single-file;
  prompt-based inference + a feedback loop over runtime errors; no knowledge graph.
  Beats PyEGo/ReadPyE on HG2.9K (+21.6% / +16.0% fixes). **Key nuance for you:** the
  baselines still fix a subset PLLM *fails* on — legacy deps, renamed packages,
  system nuances — which is exactly why the authors call for a hybrid.
- **Agentic repo-level environment setup — a CROWDED, competitive space (important
  warning).** A large 2025–2026 wave of LLM-agent systems already do repo-level
  environment building:
  - **Repo2Run** (ByteDance, NeurIPS 2025 spotlight) — bills itself as the *first*
    LLM agent to fully automate environment configuration / Dockerfiles for arbitrary
    Python repos; ~86% success on a 420-repo benchmark. Uses a repo's unit tests as
    the success signal.
  - **EnvBench**, **SetupBench**, **Installamatic**, **PIPer**, **SetupX**,
    **ExecAgent**, **FDE-Bench**, and several 2026 follow-ons (Multi-Docker-Eval,
    RAT, ResearchEnvBench, DevOps-Gym).
  - **What they all optimize:** a *working* environment (success rate / tests pass).
    **What none of them do:** explain *why* each fix was made, or target trust.
    Most also assume a repo with **unit tests**, not scientific notebooks.

**Honest implication:** you cannot make your novelty "an agent that sets up repo
environments" — that is done, at scale, by well-resourced teams. Competing head-on on
success rate against Repo2Run is a losing game for an MSc thesis. Your novelty must be
in a dimension these tools ignore.

## 4. Hybrid + transparent: proven in *adjacent* domains, not in dependency repair

The hybrid (classical/graph + LLM) *and* transparent pattern is already validated —
but for other problems, which de-risks your idea while leaving your specific
application open:

- **SecureFixAgent** (2025) — a near-perfect *template*: hybrid of a static analyzer
  (Bandit) + a local LLM in an iterative detect–repair–validate loop; the LLM gives
  fixes **with explanations**, revalidated by the static tool; a developer study
  rated explanation quality 4.5/5 for trust/adoption. But its domain is **security
  vulnerabilities**, not dependencies.
- **KGCompass**, **SGAgent** — KG + LLM for **repository-level bug repair** (use a
  code knowledge graph to find locations, LLM to patch). Hybrid works for bugs.
- **The Code Whisperer** — graph analysis + LLM to detect/explain/repair code
  smells & vulnerabilities, explicitly citing explainability, and reporting the
  hybrid beats graph-only or LLM-only.

So: hybrid + explainable repair is a recognised, valued pattern — just **not yet
applied to dependency-configuration repair, and not to notebooks.**

## 5. The gap (where your novelty sits)

Putting it together, three things are simultaneously true:
1. Classical KG tools are fast/deterministic but rigid and API-blind; **they don't
   explain to the user.**
2. LLM/agentic repo tools are flexible and strong on success rate but **opaque**,
   costly per fix, and aimed at test-bearing repos, **not notebooks.**
3. The hybrid + transparent combination is proven valuable *elsewhere* and is
   **explicitly named as future work by the leading paper in this domain**, but
   nobody has built it for **transparent, repo-level dependency repair of notebooks.**

That intersection — **transparent, hybrid (classical-first + LLM-for-hard-cases),
agentic, repo-level dependency repair, evaluated on real scientific notebooks** — is
open. That is your contribution space.

## 6. Candidate novelty statements (pick / refine with the supervisor)

Sharp options, strongest first:

- **(A) Transparency + hybrid.** *"A transparent, hybrid dependency-repair system that
  combines fast classical (knowledge-graph) resolution with LLM reasoning for the hard
  cases, and — unlike prior success-rate-only tools — explains and provides verifiable
  evidence for every repair, enabling calibrated developer trust."* (Leans on your
  existing transparency work + fills PLLM's named hybrid gap.)
- **(B) The hybrid efficiency claim.** *"Combining classical and LLM methods yields
  repair that is both more effective and more efficient than either alone — using the
  cheap deterministic method first and the LLM only where it fails."* (Directly tests
  PLLM's complementarity hypothesis; naturally measurable — which method wins, how
  often. Mirrors the `strategy_won` measurement your Phase 5 already records.)
- **(C) Domain + system.** *"The first transparent, agentic dependency-repair system
  targeting reproducibility of real scientific notebooks (not test-bearing repos),
  hybridising classical and LLM techniques."* (Leans on the notebook/GigaScience
  angle, which the agentic-repo wave ignores.)

My honest read: **(A) is the strongest** — it unifies your already-built transparency
contribution with the hybrid gap the field explicitly flagged, and it is defensible
against "isn't this just Repo2Run?" because none of those tools explain or hybridise.
(B) is the cleanest *measurable* result to report inside (A) — and you already built
the machinery to measure it. (C) is the framing that keeps you out of head-to-head
competition with the big agentic tools.

## 7. Honest risks / cautions

- **The agentic repo-setup space is competitive and well-funded.** Frame your
  contribution around transparency + hybrid + notebooks, *not* raw success rate, or
  you invite an unwinnable comparison.
- **PCREQ shows classical methods can still beat LLMs** on requirement inference — so
  "classical-first" is not a throwaway baseline; it may carry more of the load than
  expected. Good for efficiency; measure it honestly.
- **"Hybrid" must be real, not cosmetic.** You need an actual classical component
  (reuse/build on an existing KG tool such as PyEGo/ReadPyE, or a resolver), not just
  the LLM with a lookup. Deciding *which* classical tool to build on is the concrete
  next research step (the supervisor asked for this).
- Verify all numbers (PCREQ's 94%, Repo2Run's 86%, PLLM's deltas) against the papers
  before quoting them in the thesis.

## 8. What to take back to the supervisor

1. A short statement: *the leading paper (PLLM) explicitly names hybrid classical+LLM
   as future work; no one has done it transparently, at repo scale, for notebooks — so
   that is my proposed contribution.*
2. The candidate novelty statement you favour (recommend A, with B as the measurable
   result inside it).
3. One open question for her: **which classical tool to build on / hybridise with** —
   PyEGo or ReadPyE (KG tools with published artifacts), or a lighter resolver? This
   is the one remaining research decision, and she may have a preference (possibly the
   "existing tool" she named in the meeting).

## Sources
- The Last Dependency Crusade (PLLM), arXiv:2501.16191 — https://arxiv.org/pdf/2501.16191
- PyEGo, ICSE 2022 — https://doi.org/10.1145/3510003.3510127
- Knowledge-based inference / ReadPyE — https://www.researchgate.net/publication/362968228
- DockerizeMe — https://www.semanticscholar.org/paper/dc416fe1d66413011ef64a693c771be0759ab45a
- PCREQ + REQBench, arXiv:2508.02023 — https://arxiv.org/pdf/2508.02023
- Repo2Run, arXiv:2502.13681 — https://arxiv.org/abs/2502.13681
- FDE-Bench (survey of the agentic-setup landscape), arXiv:2609.27571 — https://arxiv.org/html/2609.27571
- SetupX, arXiv:2605.26186 — https://arxiv.org/html/2605.26186
- SecureFixAgent, arXiv:2509.16275 — https://arxiv.org/pdf/2509.16275
- KGCompass, arXiv:2503.21710 — https://arxiv.org/html/2503.21710v1
- SGAgent, arXiv:2602.23647 — https://arxiv.org/pdf/2602.23647
- The Code Whisperer, arXiv:2604.13114 — https://arxiv.org/pdf/2604.13114
