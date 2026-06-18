"""Street-line normalization and fuzzy matching for dedupe."""

from __future__ import annotations

import html
import re
from typing import Any

from rapidfuzz import fuzz

# US street-type and direction abbreviations (token-level expansion).
_TOKEN_EXPANSIONS: dict[str, str] = {
    "N": "NORTH",
    "S": "SOUTH",
    "E": "EAST",
    "W": "WEST",
    "NE": "NORTHEAST",
    "NW": "NORTHWEST",
    "SE": "SOUTHEAST",
    "SW": "SOUTHWEST",
    "ST": "STREET",
    "STREET": "STREET",
    "AVE": "AVENUE",
    "AV": "AVENUE",
    "AVENUE": "AVENUE",
    "BLVD": "BOULEVARD",
    "BOULEVARD": "BOULEVARD",
    "RD": "ROAD",
    "ROAD": "ROAD",
    "DR": "DRIVE",
    "DRIVE": "DRIVE",
    "LN": "LANE",
    "LANE": "LANE",
    "CT": "COURT",
    "COURT": "COURT",
    "PL": "PLACE",
    "PLACE": "PLACE",
    "TER": "TERRACE",
    "TERRACE": "TERRACE",
    "PKWY": "PARKWAY",
    "PARKWAY": "PARKWAY",
    "HWY": "HIGHWAY",
    "HIGHWAY": "HIGHWAY",
    "CIR": "CIRCLE",
    "CIRCLE": "CIRCLE",
    "TRL": "TRAIL",
    "TRAIL": "TRAIL",
}

_HOUSE_NUMBER_TOKEN_RE = re.compile(r"^(\d+(?:-\d+)?)\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9\s]+")
_WS_RE = re.compile(r"\s+")

# Mismatch cap when house numbers disagree (same pin, different building is unlikely).
_HOUSE_NUMBER_MISMATCH_CAP = 45
_RANGE_SUFFIX_MIN_SCORE = 85


def normalize_sf_address(value: Any) -> str:
    """Normalize a Salesforce or ingest address string for display."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", ", ", text, flags=re.IGNORECASE)
    return _WS_RE.sub(" ", text).strip()


def extract_street_line(address: str | None) -> str:
    """Return the street portion of an address (drop city, state, zip, country)."""
    text = normalize_sf_address(address)
    if not text:
        return ""

    # Prefer content before the first comma when the tail looks like locality metadata.
    if "," in text:
        head, tail = text.split(",", 1)
        tail_upper = tail.upper()
        if re.search(r"\b[A-Z]{2}\b", tail_upper) or re.search(r"\b\d{5}\b", tail_upper):
            text = head

    # Drop trailing state + zip when still embedded in the street segment.
    text = re.sub(r",?\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r",?\s*\d{5}(?:-\d{4})?\s*$", "", text)
    return _WS_RE.sub(" ", text).strip()


def canonicalize_street_tokens(street: str) -> str:
    """Uppercase, expand abbreviations, and collapse whitespace."""
    text = normalize_sf_address(street).upper()
    text = _NON_ALNUM_RE.sub(" ", text)
    tokens = []
    for token in _WS_RE.split(text):
        if not token:
            continue
        tokens.append(_TOKEN_EXPANSIONS.get(token, token))
    return " ".join(tokens)


def parse_house_number_token(street: str) -> tuple[int | None, int | None, int | None]:
    """Return (single, range_start, range_end) parsed from the leading house token."""
    line = extract_street_line(street).upper()
    match = re.match(r"^(\d+(?:-\d+)?)\b", line)
    if not match:
        return None, None, None

    token = match.group(1)
    if "-" in token:
        start_text, end_text = token.split("-", 1)
        return None, int(start_text), int(end_text)
    return int(token), None, None


def extract_house_number(street: str) -> str | None:
    """Return the leading house number token when present."""
    line = extract_street_line(street).upper()
    match = re.match(r"^(\d+(?:-\d+)?)\b", line)
    if not match:
        return None
    return match.group(1)


def strip_house_number(street: str) -> str:
    """Return the street line without its leading house number token."""
    line = extract_street_line(street).upper()
    stripped = re.sub(r"^\d+(?:-\d+)?\b", "", line, count=1).strip()
    return canonicalize_street_tokens(stripped)


def house_numbers_equivalent(left_street: str, right_street: str) -> bool | None:
    """Return whether house numbers refer to the same delivery point, if known."""
    left_single, left_start, left_end = parse_house_number_token(left_street)
    right_single, right_start, right_end = parse_house_number_token(right_street)

    if left_single is None and left_start is None:
        return None
    if right_single is None and right_start is None:
        return None

    if left_single is not None and right_single is not None:
        return left_single == right_single
    if left_single is not None and right_start is not None and right_end is not None:
        return right_start <= left_single <= right_end
    if right_single is not None and left_start is not None and left_end is not None:
        return left_start <= right_single <= left_end
    return False


def address_match_score(incoming_address: str, candidate_address: str) -> int:
    """Score two addresses on normalized street lines with house-number/range handling."""
    left = canonicalize_street_tokens(extract_street_line(incoming_address))
    right = canonicalize_street_tokens(extract_street_line(candidate_address))
    if not left or not right:
        return 0

    number_relation = house_numbers_equivalent(left, right)
    if number_relation is False:
        score = int(round(fuzz.WRatio(left, right)))
        return min(score, _HOUSE_NUMBER_MISMATCH_CAP)

    if number_relation is True:
        suffix_score = int(round(fuzz.WRatio(strip_house_number(left), strip_house_number(right))))
        if suffix_score >= _RANGE_SUFFIX_MIN_SCORE:
            return 100

    score = int(round(fuzz.WRatio(left, right)))
    return max(0, min(100, score))


def street_token_jaccard(left_address: str, right_address: str) -> float:
    """Jaccard similarity on street tokens (ignores house numbers)."""
    left_tokens = set(strip_house_number(extract_street_line(left_address)).split())
    right_tokens = set(strip_house_number(extract_street_line(right_address)).split())
    left_tokens.discard("")
    right_tokens.discard("")
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(intersection) / len(union)
