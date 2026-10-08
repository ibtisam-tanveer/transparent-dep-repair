"""Ask an LLM for two candidate fixes (code vs. environment) for a hard
case Phase 3 can't handle. This module is the only place that imports
`openai` or reads `OPENAI_API_KEY` — the rest of the tool stays importable
and testable without either.

Never raises: a missing key, a network failure, or a malformed response all
come back as an `LLMFixSuggestion` with `.error` set, never an exception.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .diagnose import Diagnosis

try:
    from dotenv import load_dotenv

    load_dotenv()  # picks up a local .env's OPENAI_API_KEY, if present; no-op otherwise
except ImportError:
    pass

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# Fixed, not user-configurable: temperature=0 is part of the reproducibility
# contract (PHASE5_TASK.md), so there is deliberately no env var for it.
TEMPERATURE = 0

_API_KEY_MISSING_MESSAGE = (
    "OPENAI_API_KEY is not set. Set it in your environment or in a local "
    ".env file (see README.md) to enable LLM-based repair."
)

_SYSTEM_PROMPT = """You are a Python dependency-repair assistant. A script fails \
with a dependency/API-compatibility error. Propose up to two independent \
candidate fixes: one that edits the code to work with the currently \
installed library version, and one that changes the installed library \
version so the original code works unchanged. Respond with STRICT JSON \
ONLY, no markdown fences, no commentary, matching exactly this shape:

{
  "understanding": "one sentence on what went wrong",
  "code_fix": {"applicable": true, "reason": "...", "edits": [{"find": "...", "replace": "..."}]},
  "env_fix": {"applicable": true, "reason": "...", "package": "...", "constraint": "..."}
}

Either fix may have "applicable": false with a reason instead (e.g. no code \
change can help, or no version has the symbol) — then omit "edits" or \
"package"/"constraint" for that one. "find" strings must match the source \
verbatim, character for character, since they are applied by exact-text \
replacement, not by understanding."""


@dataclass
class FixCandidate:
    """One of the two candidates the LLM returned."""

    applicable: bool
    reason: str = ""
    edits: list[dict] = field(default_factory=list)  # code_fix only
    package: str = ""  # env_fix only
    constraint: str = ""  # env_fix only


@dataclass
class LLMFixSuggestion:
    """Both candidates from a single LLM call, or an explanation of why
    there are none (missing key, network failure, malformed response)."""

    understanding: str = ""
    code_fix: FixCandidate | None = None
    env_fix: FixCandidate | None = None
    error: str = ""
    # Which model/temperature were configured for this attempt, whether or
    # not it actually produced a usable answer -- PHASE5_IMPROVEMENTS_TASK.md
    # #2, needed for the thesis methods section and for comparing runs.
    # Always set explicitly by request_fix() at construction time (not as a
    # dataclass default), so it reflects OPENAI_MODEL's value at call time.
    model: str = ""
    temperature: float = 0.0


def _strip_code_fences(text: str) -> str:
    """Some models wrap JSON in ```json ... ``` even when told not to."""
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1) if match else text


def _parse_candidate(raw: object) -> FixCandidate | None:
    if not isinstance(raw, dict):
        return None
    applicable = bool(raw.get("applicable", False))
    return FixCandidate(
        applicable=applicable,
        reason=str(raw.get("reason", "")),
        edits=[e for e in raw.get("edits", []) if isinstance(e, dict)] if applicable else [],
        package=str(raw.get("package", "")) if applicable else "",
        constraint=str(raw.get("constraint", "")) if applicable else "",
    )


def _parse_response(text: str) -> LLMFixSuggestion:
    try:
        data = json.loads(_strip_code_fences(text).strip())
    except (json.JSONDecodeError, AttributeError, TypeError):
        return LLMFixSuggestion(
            error=f"LLM response was not valid JSON: {text[:200]!r}", model=OPENAI_MODEL, temperature=TEMPERATURE
        )

    if not isinstance(data, dict):
        return LLMFixSuggestion(
            error="LLM response JSON was not an object", model=OPENAI_MODEL, temperature=TEMPERATURE
        )

    return LLMFixSuggestion(
        understanding=str(data.get("understanding", "")),
        code_fix=_parse_candidate(data.get("code_fix")),
        env_fix=_parse_candidate(data.get("env_fix")),
        model=OPENAI_MODEL,
        temperature=TEMPERATURE,
    )


def _build_user_prompt(diagnosis: Diagnosis, code: str, error_text: str) -> str:
    return (
        f"Diagnosis: kind={diagnosis.kind}, module={diagnosis.module!r}, "
        f"package={diagnosis.package!r}, symbol={diagnosis.symbol!r}\n\n"
        f"Error output:\n{error_text}\n\n"
        f"Source code:\n{code}"
    )


def request_fix(diagnosis: Diagnosis, code: str, error_text: str) -> LLMFixSuggestion:
    """Ask the model for both candidates in one call. Never raises."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return LLMFixSuggestion(error=_API_KEY_MISSING_MESSAGE, model=OPENAI_MODEL, temperature=TEMPERATURE)

    try:
        import openai
    except ImportError:
        return LLMFixSuggestion(
            error="the 'openai' package is not installed", model=OPENAI_MODEL, temperature=TEMPERATURE
        )

    try:
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=TEMPERATURE,
            # Constrains the API to only emit syntactically valid JSON --
            # without this, the model can (and does, in practice) forget to
            # escape a literal `"` inside a "find"/"replace" string (e.g.
            # code containing {"a": 3}), producing JSON that looks complete
            # but fails to parse. json_object mode makes that a server-side
            # guarantee instead of something the prompt just asks nicely for.
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(diagnosis, code, error_text)},
            ],
        )
        content = response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - any SDK/network failure -> "no fix", never a crash
        return LLMFixSuggestion(error=f"LLM call failed: {exc}", model=OPENAI_MODEL, temperature=TEMPERATURE)

    return _parse_response(content)
