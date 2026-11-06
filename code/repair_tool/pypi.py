"""Look up real package information on PyPI (stdlib urllib only, no requests).

Used to resolve an import name (what appears in an error) to an installable
PyPI package name — the two are often different (see ALIASES below).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
REQUEST_TIMEOUT = 10  # seconds

# Curated map for import names that don't match their installable PyPI name.
# A hit here is treated as verified/high-confidence (see PHASE3_ADDENDUM.md).
ALIASES = {
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "yaml": "PyYAML",
    "skimage": "scikit-image",
}


def package_exists(name: str) -> bool:
    """True if `name` is a real, installable package on PyPI.

    Never raises: any network problem (down, timeout, DNS, etc.) is treated
    as "can't confirm" -> False, since the caller's rule is never guess-install.
    """
    url = PYPI_JSON_URL.format(name=name)
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        exc.close()  # HTTPError carries an unread response body; release it
        return False
    except (urllib.error.URLError, OSError):
        # Network failure (down, DNS, timeout): can't confirm, treat as absent.
        return False


def latest_version(name: str) -> str | None:
    """Latest released version of `name` on PyPI, or None if it doesn't exist
    (or can't be confirmed — see package_exists' never-raises rationale)."""
    url = PYPI_JSON_URL.format(name=name)
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        exc.close()
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return data.get("info", {}).get("version")


def latest_version_as_of(name: str, cutoff_date: str) -> str | None:
    """The latest version of `name` that was actually released on or
    before `cutoff_date` (an ISO-ish date string, e.g. "2019-05-01" from a
    repo's last commit) -- TASK_version_decay_diagnosis.md Part B: an old
    notebook needs a version contemporary with it, not whatever is newest
    today. Defaulting to latest is the main driver of the "install at
    latest, trigger a new removed-API error" cascade found diagnosing
    kiste_smoke_v3's genuine category-A failures. Never raises: any
    network/parse problem, or a name with no release before the cutoff,
    comes back as None, same never-raise contract as latest_version.
    """
    url = PYPI_JSON_URL.format(name=name)
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        exc.close()
        return None
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    cutoff = cutoff_date[:10]
    best_version: str | None = None
    best_date = ""
    for version, files in (data.get("releases") or {}).items():
        if not files:
            continue  # a version with nothing uploaded (yanked/placeholder entry)
        upload_dates = [str(f.get("upload_time_iso_8601") or f.get("upload_time") or "")[:10] for f in files]
        upload_dates = [d for d in upload_dates if d]
        if not upload_dates:
            continue
        upload_date = min(upload_dates)
        if upload_date <= cutoff and upload_date > best_date:
            best_date, best_version = upload_date, version
    return best_version


def resolve_package_name(import_name: str) -> str | None:
    """Import name -> installable PyPI name, or None if unresolved.

    1. Curated alias map (e.g. 'sklearn' -> 'scikit-learn').
    2. Otherwise, if the import name itself exists on PyPI, use it as-is.
    3. Otherwise None — callers must not guess-install an unresolved name.
    """
    if import_name in ALIASES:
        return ALIASES[import_name]
    if package_exists(import_name):
        return import_name
    return None


def release_history(name: str) -> list[dict]:
    """Every installable release of `name`, newest first, as
    [{"version", "series", "date"}]: pre-releases and yanked/empty releases are
    dropped. `series` is major.minor ("1.4"), so a downgrade search can step by
    release series instead of by every patch. Never raises: a network problem or
    an unknown name gives [] (same contract as latest_version)."""
    from packaging.version import InvalidVersion, Version

    url = PYPI_JSON_URL.format(name=name)
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        exc.close()
        return []
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []

    out = []
    for version, files in (data.get("releases") or {}).items():
        files = [f for f in (files or []) if not f.get("yanked")]
        if not files:
            continue
        try:
            parsed = Version(version)
        except InvalidVersion:
            continue
        if parsed.is_prerelease or parsed.is_devrelease:
            continue
        dates = [str(f.get("upload_time_iso_8601") or f.get("upload_time") or "")[:10] for f in files]
        dates = [d for d in dates if d]
        out.append({"version": version, "series": f"{parsed.major}.{parsed.minor}", "date": min(dates) if dates else "", "_v": parsed})
    out.sort(key=lambda r: r["_v"], reverse=True)
    for r in out:
        del r["_v"]
    return out
