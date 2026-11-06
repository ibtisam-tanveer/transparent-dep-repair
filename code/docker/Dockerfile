# Runtime image for `--runtime docker` (TASK_containerize.md).
#
# Holds only TOOLS, never code under repair and never the repair_tool sources
# (those are copied in per container, so editing them never needs a rebuild):
#   - micromamba (from the base image): supplies old CPython (3.6/3.7 ...) and
#     builds a repo's own environment.yml, like repo2docker does;
#   - a "tools" env with uv + the libraries the in-container RPC needs
#     (nbclient/ipykernel to run notebooks, pyyaml/packaging for recipes);
#   - a C/Fortran toolchain, because old pins often have to build from source.
# Pinned to linux/amd64 by the caller (`docker build --platform ...`), so old
# x86_64 wheels resolve even on an Apple-Silicon host.
FROM mambaorg/micromamba:1.5.8

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl build-essential gfortran libgl1 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /work && chown mambauser:mambauser /work

USER mambauser
RUN micromamba create -y -n tools -c conda-forge python=3.11 pip nbformat nbclient jupyter_client jupyter_core \
        ipykernel pyyaml packaging uv \
 && micromamba clean -a -y

ENV PATH=/opt/conda/envs/tools/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin \
    MAMBA_ROOT_PREFIX=/opt/conda \
    PYTHONUNBUFFERED=1 \
    REPAIR_TOOL_PREFER_CONDA=1
WORKDIR /work
