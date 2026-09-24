"""Keyword cues read from model evidence text.

The classifier gates (cell-claim demotion, stealth promotion) and the
enrichment write gates (bucketing) both scan ``site_evidence`` /
``cell_equipment_evidence`` for telecom, stealth, and hedging language.
The cue lists live here once. The write gate deliberately accepts a few
more telecom phrasings (dish / facade / wall mounts) than the classifier's
first-pass gate, so it extends the shared base rather than redefining it.
"""

from __future__ import annotations

from typing import Any, Iterable

CELL_EVIDENCE_KEYS: tuple[str, ...] = ("cell_equipment_evidence", "site_evidence")

TELECOM_CUES: tuple[str, ...] = (
    "antenna",
    "sector",
    "rru",
    "microwave",
    "backhaul",
    "panel",
    "parapet mast",
    "radio",
    "telecom",
)
WRITE_GATE_TELECOM_CUES: tuple[str, ...] = TELECOM_CUES + (
    "dish",
    "facade mount",
    "wall-mounted",
    "wall mounted",
)

STEALTH_FORM_CUES: tuple[str, ...] = (
    "monopalm",
    "mono-palm",
    "monopine",
    "mono-pine",
    "faux palm",
    "faux-pine",
    "faux pine",
    "palm frond",
    "pine frond",
    "canister",
    "antenna bay",
    "antenna bays",
    "stealth canister",
    "pine-needle",
    "pine needle",
    "faux frond",
    "synthetic frond",
    "artificial palm",
    "artificial pine",
    "disguised as a palm",
    "disguised as a pine",
    "palm-style",
    "pine-style",
    "palm tower",
    "pine tower",
    "fake palm",
    "fake pine",
)
STEALTH_HARDWARE_CUES: tuple[str, ...] = (
    "cabinet",
    "cabinets",
    "compound",
    "fenced pad",
    "equipment pad",
    "shroud",
    "canister",
    "antenna bulge",
    "antenna bay",
    "antenna bays",
)
# Narrower disguise list the Salesforce write gate accepts for a
# claimed-site Gemini keep (bucketing._tower_claimed_keep_ok).
STEALTH_MAST_WRITE_CUES: tuple[str, ...] = (
    "monopalm",
    "monopine",
    "canister",
    "faux palm",
    "faux pine",
    "palm frond",
    "shroud",
    "antenna bay",
)

HEDGE_CUES: tuple[str, ...] = (
    "likely",
    "probably",
    "possibly",
    "possible",
    "may be",
    "might be",
    "appears to",
    "seem to",
    "cannot confirm",
    "can't confirm",
    "uncertain",
    "typical for this type",
    "low resolution",
    "too small to",
    "hard to tell",
    "unable to distinguish",
)
SPECULATIVE_STEALTH_CUES: tuple[str, ...] = (
    "likely conceal",
    "likely hides",
    "typical for",
    "as is typical",
    "probably conceal",
    "appears to conceal",
    "may conceal",
    "could conceal",
)


def evidence_text(res: dict[str, Any], keys: Iterable[str] = CELL_EVIDENCE_KEYS) -> str:
    """Join the evidence fields of a model result into one string."""
    return " ".join(str(res.get(key) or "") for key in keys)


def has_any(text: str, cues: Iterable[str]) -> bool:
    lowered = (text or "").lower()
    return any(cue in lowered for cue in cues)


def has_telecom_cue(text: str) -> bool:
    return has_any(text, TELECOM_CUES)


def has_stealth_form(text: str) -> bool:
    if has_any(text, STEALTH_FORM_CUES):
        return True
    # Pairing "palm"/"pine" with stealth is specific; "palm"+"tower" is not
    # (street palms next to a real monopole).
    lowered = (text or "").lower()
    return "stealth" in lowered and ("palm" in lowered or "pine" in lowered)


def has_stealth_hardware(text: str) -> bool:
    return has_any(text, STEALTH_HARDWARE_CUES)
