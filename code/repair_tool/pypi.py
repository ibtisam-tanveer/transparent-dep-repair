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
