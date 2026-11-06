# Container runtime (`--runtime docker`)

Runs the repair **actions** inside a Linux container so the tool is
machine-independent and old x86_64 packages install even on an Apple-Silicon
host. Follows the Binder/repo2docker idea: the repo's own declared recipe
(`environment.yml` / `requirements.txt` / `runtime.txt`) decides the
interpreter and packages.

## Requirements

- Docker (Docker Desktop on macOS/Windows, or the Docker engine on Linux), with the
  daemon running. `run_eval.py --runtime docker` checks this first and stops with a
  clear message if it is not usable.
- Internet access from the container (PyPI, conda-forge, GitHub for `uv` Pythons).
- On Apple Silicon: `linux/amd64` runs under emulation (Rosetta in recent Docker
  Desktop). Correct but slower; budget for it (see below).
- The kiste/LLM calls need the university VPN on the **host** — the container itself
  never talks to the model.

## What runs where

| host | container |
|---|---|
| the agent loop and every LLM call | building the environment (interpreter provisioning, declared dependencies, `postBuild`) |
| PyPI lookups, diagnosis of captured output | running the target script/notebook |
| result rows, checkpoints | `pip`/`conda` installs, the target-file edits |

Each action is one `docker exec ... python -m repair_tool.container_rpc` (JSON in, one
`RPC_RESULT:` line out). The container runs the **same** functions as the host path, so
interpreter repair, declared-deps-first, era-capped installs, working-directory
resolution, the async-kernel / process-group / watchdog hang fixes and provenance all
apply unchanged. One container per repo; it is removed afterwards (also if the worker is
killed — the harness runs `docker rm -f`). The repo is **copied** in without `.git`
(not bind-mounted) and the edited file is copied back after each notebook.

## Usage

```bash
python evaluation/run_eval.py --failures-csv evaluation/samples/pilot_sample.csv \
    --sample 0 --seed 42 --in-scope-confirmed-only --tag my_pilot \
    --runtime docker --workers 2
```

- `--runtime host|docker` (default `host`) — the host-vs-container comparison is a flag.
- `--platform linux/amd64` (default) — a configurable default, recorded on every row
  (`container_platform`). Nothing else in the code hard-codes it. A platform that cannot
  be provided (GPU/CUDA, arm-only) is reported as out-of-scope, not chased.
- `--container-scale 3.0` — multiplies the per-cell limit and the per-repo budget to
  account for emulation. Measured on this Mac (Rosetta): CPU-bound Python ~1.8x slower,
  `pip install numpy pandas` ~4.5x slower; 3.0 is a middle value. Native x86_64 Linux
  should use 1.0.

The runtime image (`repair-tool-runtime:1-linux-amd64`) is built on first use from
`docker/Dockerfile` and cached; it contains micromamba, a `tools` env (uv, nbclient,
ipykernel, ...) and a C/Fortran toolchain — never the repair code (copied in per
container) and never code under repair.

## Inside the container

- **Python versions:** `environment.yml` is solved with micromamba (as repo2docker does).
  For other repos micromamba is tried first for the *exact* requested minor (it has
  3.6/3.7 on linux-64; `uv` has nothing below 3.8), then `uv` (nearest newer minor).
  Which one was used is recorded (`python_mechanism`, `python_version_requested/actual`).
- **Kernel pin:** under Rosetta, `ipykernel >= 6.29` starts but never answers its first
  message (measured: 6.20.2 and 6.16.2 are ready in <1 s), so every notebook would hang.
  The container pins `ipykernel==6.16.2` for the *kernel* (`REPAIR_TOOL_IPYKERNEL_SPEC`);
  a newer one already in the target environment is replaced, an older one is left alone.
  Recorded as `container_kernel_pin`. A native x86_64 host does not need it.

## Honest limits

- Containers fix the host-architecture mismatch. They do not fix packages with no build
  on any `linux/amd64` + target-Python combination, nor conda-only packages missing for
  the target — those stay out-of-scope and are reported.
- Emulation is not a faithful proxy for a native x86_64 machine: it can be slower, and
  it changes behaviour (the kernel hang above; `pyferret.start()` hangs inside a kernel
  in this setup — cause not isolated). Results should say which platform produced them.
- `postBuild` and the notebooks are arbitrary repo code. In the container that is
  contained to the container; on the host (`--runtime host`) it runs with your user's
  permissions.
