"""Typed environment-variable readers shared by enrichment and the classifier.

Every module used to carry its own ``_env_flag`` / ``float(os.environ.get(...))``
copy with slightly different truthy sets and no handling for malformed values.
These helpers are the single definition: blank or unparsable values fall back
to the default instead of raising at import time.
"""

from __future__ import annotations

import os

TRUE_VALUES = frozenset({"1", "true", "yes"})


def env_str(name: str, default: str = "") -> str:
    """Stripped value, or ``default`` when unset or blank."""
    value = (os.environ.get(name) or "").strip()
    return value or default


def env_flag(name: str, default: bool | str = False) -> bool:
    """True for 1/true/yes (case-insensitive). ``default`` may be a bool or "0"/"1"."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        if isinstance(default, str):
            return default.strip().lower() in TRUE_VALUES
        return bool(default)
    return raw in TRUE_VALUES


def env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(float(raw))
    except ValueError:
        return int(default)


def env_csv(name: str) -> list[str] | None:
    """Comma-separated list with blanks dropped; None when unset or empty."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    return parts or None
