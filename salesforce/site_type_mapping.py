"""Map classifier + permit signals to Salesforce Site_Type__c picklist values."""

from __future__ import annotations

import re
from typing import Any

TOWER_SUBTYPE_VALUES: tuple[str, ...] = (
    "monopole",
    "guyed",
    "self_support",
    "stealth",
    "steeple",
    "water_tower",
    "silo",
    "flagpole",
    "smokestack",
    "other_tower",
    "unclear",
)

TOWER_SUBTYPE_TO_SF: dict[str, str] = {
    "monopole": "Monopole",
    "guyed": "Guyed Tower",
    "self_support": "Self Support",
    "stealth": "Stealth",
    "steeple": "Steeple",
    "water_tower": "Water Tower",
    "silo": "Silo",
    "flagpole": "Flagpole",
    "smokestack": "Smokestack",
    "other_tower": "Self Support",
    "unclear": "Self Support",
}

SITE_TYPE_TO_SF: dict[str, str] = {
    "rooftop": "Rooftop",
    "other": "Equipment",
    "small_cell": "Small Cell",
    "das": "DAS",
    "billboard": "Billboard",
}

PERMIT_SITE_TYPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmonopole\b", re.I), "Monopole"),
    (re.compile(r"\bguyed\b", re.I), "Guyed Tower"),
    (re.compile(r"\b(self[\s-]?support|lattice)\b", re.I), "Self Support"),
    (re.compile(r"\bstealth\b", re.I), "Stealth"),
    (re.compile(r"\bsteeple\b", re.I), "Steeple"),
    (re.compile(r"\bwater[\s-]?tower\b", re.I), "Water Tower"),
    (re.compile(r"\bsilo\b", re.I), "Silo"),
    (re.compile(r"\bflagpole\b", re.I), "Flagpole"),
    (re.compile(r"\bsmokestack\b", re.I), "Smokestack"),
    (re.compile(r"\bsmall[\s-]?cell\b", re.I), "Small Cell"),
    (re.compile(r"\bdas\b", re.I), "DAS"),
    (re.compile(r"\brooftop\b", re.I), "Rooftop"),
)


def normalize_tower_subtype(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    aliases = {
        "selfsupport": "self_support",
        "lattice": "self_support",
        "guyed_tower": "guyed",
        "guyed_mast": "guyed",
        "watertower": "water_tower",
        "other": "other_tower",
    }
    text = aliases.get(text, text)
    if text in TOWER_SUBTYPE_TO_SF:
        return text
    return None


def site_type_from_permit_text(text: str | None) -> str | None:
    if not text:
        return None
    for pattern, sf_value in PERMIT_SITE_TYPE_PATTERNS:
        if pattern.search(text):
            return sf_value
    return None


def site_type_from_permit_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    chunks: list[str] = []
    for key in (
        "description",
        "work_description",
        "permit_description",
        "project_description",
        "remarks",
        "notes",
        "permit_type",
        "type",
    ):
        value = metadata.get(key)
        if value:
            chunks.append(str(value))
    return site_type_from_permit_text(" ".join(chunks))


def map_site_type_for_upload(
    classified: dict[str, Any] | None = None,
    *,
    permit_metadata: dict[str, Any] | None = None,
    explicit_site_type: str | None = None,
) -> str:
    """Resolve Salesforce Site_Type__c from classifier output and permit hints."""
    if explicit_site_type:
        return explicit_site_type

    classified = classified or {}
    permit_hint = site_type_from_permit_metadata(permit_metadata)

    raw_site_type = str(classified.get("site_type") or "").strip().lower()
    tower_subtype = normalize_tower_subtype(classified.get("tower_subtype"))

    if raw_site_type == "tower":
        if tower_subtype and tower_subtype not in {None, "unclear"}:
            return TOWER_SUBTYPE_TO_SF[tower_subtype]
        if permit_hint and permit_hint not in {"Rooftop", "Small Cell", "DAS"}:
            return permit_hint
        if tower_subtype:
            return TOWER_SUBTYPE_TO_SF[tower_subtype]
        return TOWER_SUBTYPE_TO_SF["unclear"]

    if raw_site_type in SITE_TYPE_TO_SF:
        mapped = SITE_TYPE_TO_SF[raw_site_type]
        if raw_site_type == "other" and permit_hint:
            return permit_hint
        return mapped

    if permit_hint:
        return permit_hint

    if raw_site_type in {value.lower() for value in _ALL_SF_SITE_TYPES()}:
        for sf_value in _ALL_SF_SITE_TYPES():
            if sf_value.lower() == raw_site_type:
                return sf_value
    return ""


def _ALL_SF_SITE_TYPES() -> tuple[str, ...]:
    values = set(SITE_TYPE_TO_SF.values()) | set(TOWER_SUBTYPE_TO_SF.values())
    return tuple(sorted(values))
