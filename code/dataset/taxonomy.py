"""Classify a GigaScience `executions.reason` string into the
dependency-failure taxonomy (A-E) + a confidence tier -- see
DEPENDENCY_FAILURE_TAXONOMY.md for the full scheme and rationale.

Why this can't just reuse repair_tool.diagnose.diagnose(): that classifier
needs the exception *message* content to tell its kinds apart (e.g. "No
module named 'X'", "module 'X' has no attribute 'Y'"), but this database's
`reason` column, for the real execution-exception population, is
overwhelmingly the **bare exception class name alone** (confirmed directly
against the 2021 run in dataset/NOTES.md, and again here) -- feeding that
through diagnose() classifies everything as "unknown". This module is a
separate, dataset-specific classifier built from the real distribution of
`reason` values actually observed in the 2023 db (dataset/NOTES_2023.md),
not a guess.

Two layers, checked in order:
1. A handful of message-content override rules for the small set of
   high-precision textual signals that *do* appear (e.g. "No module named"
   -> A confirmed; a file path + "does not exist" -> E confirmed).
2. A default (category, tier) per exception class name, used when no
   override fires -- e.g. a bare `AttributeError` with no message defaults
   to C/candidate, exactly the taxonomy doc's own example.

Every row gets exactly one category and one tier. Nothing here touches
`repair_tool/` -- this is dataset labelling, not repair logic.
"""

from __future__ import annotations

import re

CATEGORY_A = "A"  # missing dependency
CATEGORY_B = "B"  # moved/renamed import
CATEGORY_C = "C"  # removed/changed API
CATEGORY_D = "D"  # incompatible/version conflict
CATEGORY_E = "E"  # other/not a dependency failure

TIER_CONFIRMED = "confirmed"
TIER_CANDIDATE = "candidate"
TIER_EXCLUDED = "excluded"

CATEGORIES = (CATEGORY_A, CATEGORY_B, CATEGORY_C, CATEGORY_D, CATEGORY_E)
TIERS = (TIER_CONFIRMED, TIER_CANDIDATE, TIER_EXCLUDED)


def split_reason(reason_raw: str | None) -> tuple[str, str]:
    """'ModuleNotFoundError' -> ('ModuleNotFoundError', '').
    'FileNotFoundError: [Errno 2] ...' -> ('FileNotFoundError', '[Errno 2] ...').
    None (no reason recorded at all) -> ('', '').
    """
    if not reason_raw:
        return "", ""
    if ":" in reason_raw:
        class_name, _, message = reason_raw.partition(":")
        return class_name.strip(), message.strip()
    return reason_raw.strip(), ""


# Message-content overrides, checked before the class-name default --
# each entry is (pattern over the message text, category, tier). Order
# matters: the first match wins.
_MESSAGE_OVERRIDES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"no module named", re.I), CATEGORY_A, TIER_CONFIRMED),
    (re.compile(r"cannot import name", re.I), CATEGORY_B, TIER_CONFIRMED),
    (
        re.compile(
            r"does not exist|no such file|file exists|unable to open file|"
            r"could not open|no such file or no access",
            re.I,
        ),
        CATEGORY_E,
        TIER_CONFIRMED,
    ),
    (
        re.compile(r"please install|requires? .* to be installed|not installed", re.I),
        CATEGORY_A,
        TIER_CANDIDATE,
    ),
    (
        re.compile(
            r"connection|urlopen|http error|httpconnectionpool|httpsconnectionpool|"
            r"max retries exceeded|tunnel connection|proxyerror|certificateerror|"
            r"ratelimitexceeded|client error|server error",
            re.I,
        ),
        CATEGORY_E,
        TIER_CONFIRMED,
    ),
    (
        re.compile(
            r"undefined symbol|incompatible|version .*(required|expected)|requires .* but|"
            r"wrong version|binary incompatib",
            re.I,
        ),
        CATEGORY_D,
        TIER_CANDIDATE,
    ),
]

# Exception class name -> default (category, tier), used when no message
# override above fires. Built from the real distinct `reason` values
# observed in the 2023 db (dataset/NOTES_2023.md) -- not a guess at what
# exceptions "might" occur.
_CLASS_DEFAULTS: dict[str, tuple[str, str]] = {
    "ModuleNotFoundError": (CATEGORY_A, TIER_CONFIRMED),
    # Bare ImportError (no message) matches the GigaScience authors' own
    # get_repro_missing_dependencies() grouping (dataset/NOTES.md) -- most
    # come from Python 2-era executions, where plain `import missing_pkg`
    # raises ImportError, not ModuleNotFoundError (a 3.6+ subclass). Still
    # candidate, not confirmed: a bare ImportError with no message could
    # also be a moved/renamed import (B), which the message alone can't
    # rule out.
    "ImportError": (CATEGORY_A, TIER_CANDIDATE),
    "AttributeError": (CATEGORY_C, TIER_CANDIDATE),
    "TypeError": (CATEGORY_E, TIER_CANDIDATE),
    "FileNotFoundError": (CATEGORY_E, TIER_CONFIRMED),
    "FileExistsError": (CATEGORY_E, TIER_EXCLUDED),
    "IOError": (CATEGORY_E, TIER_CONFIRMED),
    "OSError": (CATEGORY_E, TIER_CANDIDATE),
    "CalledProcessError": (CATEGORY_D, TIER_CANDIDATE),
    "NameError": (CATEGORY_E, TIER_CANDIDATE),
    "ValueError": (CATEGORY_E, TIER_CANDIDATE),
    "KeyError": (CATEGORY_E, TIER_CANDIDATE),
    "IndexError": (CATEGORY_E, TIER_CANDIDATE),
    "SyntaxError": (CATEGORY_E, TIER_EXCLUDED),
    "IndentationError": (CATEGORY_E, TIER_EXCLUDED),
    "AssertionError": (CATEGORY_E, TIER_CANDIDATE),
    "RuntimeError": (CATEGORY_E, TIER_CANDIDATE),
    "LZMAError": (CATEGORY_E, TIER_CANDIDATE),
    "PermissionError": (CATEGORY_E, TIER_CANDIDATE),
    "UsageError": (CATEGORY_E, TIER_EXCLUDED),
    "InvalidURL": (CATEGORY_E, TIER_CONFIRMED),
    "HTTPError": (CATEGORY_E, TIER_CONFIRMED),
    "ConnectionError": (CATEGORY_E, TIER_CONFIRMED),
    "ConnectionRefusedError": (CATEGORY_E, TIER_CONFIRMED),
    "URLError": (CATEGORY_E, TIER_CONFIRMED),
    "ProxyError": (CATEGORY_E, TIER_CONFIRMED),
    "CertificateError": (CATEGORY_E, TIER_CONFIRMED),
    "RequestException": (CATEGORY_E, TIER_CONFIRMED),
    "RateLimitExceededException": (CATEGORY_E, TIER_CONFIRMED),
    "NoValidConnectionsError": (CATEGORY_E, TIER_CONFIRMED),
    "MissingSchema": (CATEGORY_E, TIER_CANDIDATE),
    "ExecutableNotFound": (CATEGORY_E, TIER_CANDIDATE),
    "ZeroDivisionError": (CATEGORY_E, TIER_EXCLUDED),
    "UnpicklingError": (CATEGORY_E, TIER_CANDIDATE),
    "UnicodeDecodeError": (CATEGORY_E, TIER_EXCLUDED),
    "JSONDecodeError": (CATEGORY_E, TIER_EXCLUDED),
    "XLRDError": (CATEGORY_E, TIER_CANDIDATE),
    "ParserError": (CATEGORY_E, TIER_EXCLUDED),
    "DataSourceNotFoundError": (CATEGORY_E, TIER_CONFIRMED),
    "PackageNotInstalledError": (CATEGORY_A, TIER_CANDIDATE),
    "PDFInfoNotInstalledError": (CATEGORY_A, TIER_CANDIDATE),
    "QtBindingsNotFoundError": (CATEGORY_A, TIER_CANDIDATE),
    "SolverNotFound": (CATEGORY_D, TIER_CANDIDATE),
    "ManifestVersionError": (CATEGORY_D, TIER_CANDIDATE),
    "ConfigException": (CATEGORY_E, TIER_EXCLUDED),
    "NotEnoughParticles": (CATEGORY_E, TIER_EXCLUDED),
    "LimeError": (CATEGORY_E, TIER_EXCLUDED),
    "ArbSweepProtocolException": (CATEGORY_E, TIER_EXCLUDED),
    "StdinNotImplementedError": (CATEGORY_E, TIER_EXCLUDED),
    "TclError": (CATEGORY_E, TIER_EXCLUDED),
    "SystemError": (CATEGORY_E, TIER_CANDIDATE),
    "OptionError": (CATEGORY_E, TIER_EXCLUDED),
    "Exception": (CATEGORY_E, TIER_EXCLUDED),
    "error": (CATEGORY_E, TIER_EXCLUDED),
}

# Anything whose class name isn't in the table above (the long tail of
# one-off exception names actually observed is large -- see
# dataset/NOTES_2023.md) defaults here: out of scope, excluded, until
# someone inspects it specifically. Matches the taxonomy doc's own stance
# that an unrecognised reason should be re-examined, not silently counted
# as a dependency failure.
_DEFAULT_UNKNOWN: tuple[str, str] = (CATEGORY_E, TIER_EXCLUDED)


def classify(reason_raw: str | None) -> tuple[str, str, str, str]:
    """Classify one `executions.reason` value.

    Returns (category, tier, class_name, message). Never raises: an empty,
    None, or wholly unrecognised reason comes back as (E, excluded, "", "")
    or (E, excluded, class_name, message) rather than an exception.
    """
    class_name, message = split_reason(reason_raw)

    for pattern, category, tier in _MESSAGE_OVERRIDES:
        if message and pattern.search(message):
            return category, tier, class_name, message

    category, tier = _CLASS_DEFAULTS.get(class_name, _DEFAULT_UNKNOWN)
    return category, tier, class_name, message
