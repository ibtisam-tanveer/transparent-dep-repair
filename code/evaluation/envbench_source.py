"""Where an EnvBench repository comes from (TASK_envbench_evaluation.md).

An EnvBench entry is `owner/repo` at ONE pinned commit. The failures CSV carries it as

    pinned:<40-hex sha>:https://github.com/<owner>/<repo>.git

and this module turns that into a working tree:

1. shallow-fetch exactly that commit from GitHub (`git fetch --depth 1 origin <sha>`): one commit, a few MB
   instead of the 51 GB of EnvBench's full-history archives. The single commit is kept (`.git`): the harness derives
   its default era cap for reactive installs from the repository's last-commit date, as in every other run;
2. if GitHub no longer serves the repository (renamed / deleted / force-pushed away), fall back to EnvBench's own
   archive of it on Hugging Face (pinned to the dataset revision recorded in the sample metadata), check the
   commit out of the archive's history, and squash it into one commit that keeps the original commit date.
   Archives above MAX_ARCHIVE_BYTES are not downloaded.

Nothing here runs repository code. The tree is copied into the container by the normal harness path; the check
file (evaluation/envbench_check.py) is injected afterwards by `inject_check`.
"""

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

PREFIX = "pinned:"
HF_DATASET = "JetBrains-Research/EnvBench"
# The dataset revision the sample was built from (evaluation/samples/envbench_py_all.meta.json records the same value).
HF_REVISION = "d1e96e6b10335cad40ac7b4f709f46a2c579765a"
HF_ARCHIVE_URL = "https://huggingface.co/datasets/%s/resolve/%s/repos/final/python/%s.tar.gz"
MAX_ARCHIVE_BYTES = 3 * 1024 ** 3
FETCH_TIMEOUT = 900
# Media/archives cannot influence which imports resolve. A repository with more than LEAN_THRESHOLD of them (the
# only one in the EnvBench Python list is commaai/comma10k: ~25,000 images, 13 GB) is checked out WITHOUT them, and
# the run log says so. Everything else is checked out in full.
MEDIA_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".mp4", ".mov", ".avi", ".mkv",
                    ".zip", ".gz", ".7z")
LEAN_THRESHOLD = 500
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$")
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}


def spec_for(repo: str, sha: str) -> str:
    return f"{PREFIX}{sha}:https://github.com/{repo}.git"


def parse_spec(spec: str) -> tuple[str, str, str]:
    """(sha, url, 'owner/repo'). Raises ValueError for anything that is not exactly a pinned GitHub entry --
    the value ends up in a `git fetch` command line, so it is validated, never interpolated blindly."""
    if not spec.startswith(PREFIX):
        raise ValueError("not a pinned source")
    sha, sep, url = spec[len(PREFIX):].partition(":")
    if not sep or not _SHA_RE.match(sha):
        raise ValueError(f"bad commit in pinned source: {sha!r}")
    match = _URL_RE.match(url)
    if not match:
        raise ValueError(f"bad repository url in pinned source: {url!r}")
    return sha, url, f"{match.group(1)}/{match.group(2)}"


def _git(args: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **_GIT_ENV})


def _fail(tmpdir: str, args: list[str], done: subprocess.CompletedProcess) -> tuple[None, str]:
    shutil.rmtree(tmpdir, ignore_errors=True)
    return None, f"git {args[0]} failed: {(done.stdout + done.stderr).strip()[:300]}"


def fetch_pinned(url: str, sha: str, timeout: int = FETCH_TIMEOUT) -> tuple[str | None, str]:
    """Shallow, blob-less fetch of the one commit (cheap: trees only), then a checkout of it -- without media files
    when the tree has more than LEAN_THRESHOLD of them (see MEDIA_EXTENSIONS)."""
    tmpdir = tempfile.mkdtemp(prefix="repair_tool_envbench_")
    try:
        for args in (["init", "-q"], ["remote", "add", "origin", url]):
            done = _git(args, tmpdir, timeout)
            if done.returncode != 0:
                return _fail(tmpdir, args, done)
        fetch = ["fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", sha]
        done = _git(fetch, tmpdir, timeout)
        if done.returncode != 0:  # a server that refuses filters: plain shallow fetch
            fetch = ["fetch", "-q", "--depth", "1", "origin", sha]
            done = _git(fetch, tmpdir, timeout)
            if done.returncode != 0:
                return _fail(tmpdir, fetch, done)
        listing = _git(["ls-tree", "-r", "--name-only", "FETCH_HEAD"], tmpdir, timeout)
        media = [n for n in listing.stdout.splitlines() if n.lower().endswith(MEDIA_EXTENSIONS)]
        note = "fetched the pinned commit"
        if len(media) > LEAN_THRESHOLD:
            patterns = ["/*"] + [f"!*{ext}" for ext in MEDIA_EXTENSIONS]
            done = _git(["sparse-checkout", "set", "--no-cone", *patterns], tmpdir, timeout)
            if done.returncode != 0:
                return _fail(tmpdir, ["sparse-checkout"], done)
            note += f" (lean checkout: {len(media)} media files omitted)"
            print(f"[envbench_source] {url}@{sha[:10]}: {note}", file=sys.stderr, flush=True)
        done = _git(["checkout", "-q", "FETCH_HEAD"], tmpdir, timeout)
        if done.returncode != 0:
            return _fail(tmpdir, ["checkout"], done)
        return tmpdir, note
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, f"git timed out after {timeout}s"
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, f"could not run git: {exc}"


def squash_to_single_commit(root: str, timeout: int = FETCH_TIMEOUT) -> str:
    """Replace the checkout's history by ONE commit with the original commit date ('' on success, else why not):
    the same shape `fetch_pinned` produces, so every repository reaches the harness identically."""
    date = _git(["log", "-1", "--format=%cI"], root, timeout).stdout.strip()
    if not date:
        return "no commit date"
    shutil.rmtree(os.path.join(root, ".git"), ignore_errors=True)
    env = {**os.environ, **_GIT_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
           "GIT_AUTHOR_NAME": "envbench", "GIT_AUTHOR_EMAIL": "envbench@localhost",
           "GIT_COMMITTER_NAME": "envbench", "GIT_COMMITTER_EMAIL": "envbench@localhost"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--no-gpg-sign", "-m", "pinned revision"]):
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout, env=env)
        if done.returncode != 0:
            return (done.stdout + done.stderr).strip()[:200]
    return ""


def fetch_hf_archive(repo: str, sha: str, timeout: int = FETCH_TIMEOUT) -> tuple[str | None, str]:
    """EnvBench's own archive of the repository (full history), checked out at the pinned commit."""
    name = repo.replace("/", "__")
    url = HF_ARCHIVE_URL % (HF_DATASET, HF_REVISION, name)
    workdir = tempfile.mkdtemp(prefix="repair_tool_envbench_hf_")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as head:
            size = int(head.headers.get("Content-Length") or 0)
        if size > MAX_ARCHIVE_BYTES:
            return None, f"HF archive too large ({size / 1e9:.1f} GB), not downloaded"
        archive = os.path.join(workdir, "repo.tar.gz")
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(archive, "wb") as out:
            shutil.copyfileobj(resp, out)
        with tarfile.open(archive, "r:gz") as tar:
            # data filter: refuses absolute paths, parent-directory escapes and special files (Python >= 3.12)
            tar.extractall(workdir, **({"filter": "data"} if sys.version_info >= (3, 12) else {}))
        os.remove(archive)
        roots = [os.path.dirname(os.path.join(dp, ".git")) for dp, dn, _ in os.walk(workdir) if ".git" in dn]
        if not roots:
            return None, "HF archive has no git checkout"
        root = min(roots, key=len)
        done = _git(["checkout", "-q", "-f", sha], root, timeout)
        if done.returncode != 0:
            return None, f"commit not in the HF archive: {(done.stdout + done.stderr).strip()[:300]}"
        squashed = squash_to_single_commit(root, timeout)
        if squashed:
            return None, f"could not squash the archive checkout: {squashed}"
        final = tempfile.mkdtemp(prefix="repair_tool_envbench_")
        shutil.copytree(root, final, symlinks=False, dirs_exist_ok=True)
        return final, "from the EnvBench Hugging Face archive"
    except (OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        return None, f"HF archive failed: {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def obtain(spec: str) -> tuple[str | None, str]:
    """(path | None, log), never raises: the same contract as run_eval.clone_repo."""
    try:
        sha, url, repo = parse_spec(spec)
    except ValueError as exc:
        return None, str(exc)
    path, log = fetch_pinned(url, sha)
    if path is not None:
        return path, log
    hf_path, hf_log = fetch_hf_archive(repo, sha)
    if hf_path is not None:
        return hf_path, f"{hf_log} (GitHub: {log})"
    return None, f"GitHub: {log}; HF fallback: {hf_log}"


def inject_check(clone_path: str) -> str:
    """Put EnvBench's check next to the repository's own files; returns its relative path."""
    from evaluation import envbench_check

    source = os.path.abspath(envbench_check.__file__)
    shutil.copyfile(source, os.path.join(clone_path, envbench_check.TARGET_NAME))
    return envbench_check.TARGET_NAME
