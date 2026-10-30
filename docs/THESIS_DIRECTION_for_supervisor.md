# Thesis Direction

**AI-Driven Transparent Repair of Software Dependency Configurations**
Muhammad Ibtisam Tanveer · Supervisor: Dr. Sheeba Samuel

This note sets out the direction I am taking the thesis, following our discussion
about repository-level scope, a hybrid classical + LLM approach, and an agentic
design. It reflects the background research you asked me to do on the existing
(classical) tools and on where the novelty lies.

## Problem and motivation

Published scientific code — especially Jupyter notebooks released with papers —
frequently fails to run again later because its dependency configuration has
decayed: packages are missing, imports have moved, or APIs have been removed or
changed across versions. This breaks computational reproducibility. The problem is
real at scale, as documented in the GigaScience reproducibility study.

## What is already built

A working, tested repair engine (≈150 automated tests). Given a broken project it
runs the code, diagnoses the dependency error, proposes a fix (installing a package,
pinning a version, or adapting the code), verifies the fix by actually re-running,
and records why each change was made. It handles both scripts and notebooks, and can
already set up and diagnose a whole repository. This is the foundation; the direction
below builds on it rather than replacing it.

## What the research shows (the gap)

- **Classical tools** for Python dependency inference (DockerizeMe, PyEGo, ReadPyE,
  PyCRE, SnifferDog) are knowledge-graph based: fast and deterministic, but rigid,
  dependent on a maintained knowledge base, and — importantly — they do not explain
  their decisions to the developer.
- **LLM / agentic tools** for repository environment setup (Repo2Run, EnvBench,
  SetupAgent and a large 2025–26 wave) are flexible and strong on success rate, but
  they are opaque, optimise only for "does it run", and target test-bearing software
  repositories — **not scientific notebooks**.
- **Hybrid (classical + LLM) and explainable repair** is proven valuable in adjacent
  areas (e.g. security-vulnerability repair, repository-level bug repair) but has not
  been applied to dependency repair or to notebooks. Notably, the leading LLM paper
  in this exact area (PLLM, "The Last Dependency Crusade") explicitly names a hybrid
  of LLM reasoning with classical knowledge-graph methods as open future work.

So there is a clear gap at the intersection: **transparent, hybrid, repository-level
dependency repair of real scientific notebooks.**

## Proposed direction

A **transparent, neuro-symbolic, agentic** system for repository-level dependency
repair. In plain terms:

- **Agentic:** an LLM agent decides what to do and calls tools, rather than following
  a fixed script. The work is divided across a small set of specialised agents
  (diagnosis, repair, verification).
- **Hybrid (neuro-symbolic):** the agent uses reliable classical tools (package
  metadata and resolution, with the option to incorporate an established
  knowledge-graph tool such as PyEGo/ReadPyE) for facts, and the LLM only for the
  harder reasoning the classical tools cannot do — combining their complementary
  strengths, as the literature recommends.
- **Verified:** no fix is accepted unless it is confirmed by a deterministic check
  (re-running the project, or metadata/solver confirmation). The agent may propose;
  only the checker may approve.
- **Transparent:** every repair decision is recorded with its reason, its source,
  and — crucially — *how it was established*, on a calibrated scale from
  "deterministically verified" down to "unverified model suggestion". The result is
  a complete, auditable repair trace a developer can trust and inspect.

## Where the novelty lies

The agentic/hybrid architecture itself is not claimed as novel — such architectures
exist in recent work. The contribution is twofold: (1) the **application** —
transparent dependency repair of scientific notebooks at repository scale, which is
unaddressed; and (2) **transparency as a first-class, provenance-tagged,
trust-calibrated output** of such a system, together with an evaluation of whether
that transparency actually helps developers trust and verify automated repairs.

## Approach and scope (staged, to stay achievable)

To keep the work realistic within the thesis timeframe, I am staging it:
1. Expose the existing engine as tools an agent can call; replace the fixed decision
   loop with a single tool-calling agent that records a provenance-tagged trace.
2. Grow to a small set of specialised agents (diagnosis / repair / verification).
3. Extend from single files to repository scale.
4. Optionally incorporate an established classical knowledge-graph tool
   (PyEGo/ReadPyE) as an additional grounded tool, if time allows.

The deterministic baseline is kept alongside the agentic version so the two can be
compared.

## Evaluation (planned)

- **Primary dataset:** the GigaScience 2023 notebook corpus, with a justified
  dependency-failure taxonomy (I will report repair results per category).
- **Secondary (if feasible):** EnvBench, to show the approach is not overfitted to
  one dataset.
- **Effectiveness:** repair success rate, compared against the deterministic baseline
  and prior work.
- **Transparency / trust (RQ4):** whether the explanations help developers judge
  fixes — via a small developer study if the timeframe allows, or an automated proxy
  otherwise.

I would welcome any steer you have on the direction, but otherwise I will proceed
with stage 1.
