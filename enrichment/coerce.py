"""Value coercion for CSV / JSONL / Salesforce rows.

Detail rows round-trip through CSV, so booleans arrive as "True"/"true"/"1",
numbers as strings, and blanks as "" or "nan". These are the one set of
parsers every module uses.
"""

from __future__ import annotations

from typing import Any


def is_missing(value: Any) -> bool:
    """True for None, blank, "nan", or float NaN."""
    if value is None:
        return True
    if isinstance(value, float):
        return value != value
    if isinstance(value, str):
        text = value.strip()
        return not text or text.lower() == "nan"
    return False


def to_float(value: Any) -> float | None:
    if is_missing(value) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def to_int(value: Any, default: int = 0) -> int:
    number = to_float(value)
    return default if number is None else int(number)


def to_bool(value: Any) -> bool | None:
    """True/False for recognizable values, None when missing or unrecognized."""
    if isinstance(value, bool):
        return value
    if is_missing(value):
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def is_true(value: Any) -> bool:
    return to_bool(value) is True


def lower_text(value: Any) -> str:
    return str(value or "").strip().lower()


def text_or_none(value: Any, max_len: int | None = None) -> str | None:
    """Stripped text (optionally truncated), or None when blank."""
    if is_missing(value):
        return None
    text = str(value).strip()
    return text[:max_len] if max_len else text
