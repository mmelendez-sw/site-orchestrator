"""Parse US-style addresses into Salesforce upload components."""

from __future__ import annotations

import re

from dedupe.address_match import (
    collapse_osm_address,
    extract_city_from_address,
    extract_street_line_raw,
    is_osm_verbose_address,
    strip_leading_poi,
)
from ingest.address_utils import parse_zip_from_address

_WS_RE = re.compile(r"\s+")
_DIRECTIONALS = frozenset({"N", "S", "E", "W", "NE", "NW", "SE", "SW"})
_STREET_SUFFIX_MAP = {
    "ST": "St",
    "STREET": "Street",
    "AVE": "Ave",
    "AV": "Av",
    "AVENUE": "Avenue",
    "RD": "Rd",
    "ROAD": "Road",
    "BLVD": "Blvd",
    "DR": "Dr",
    "LN": "Ln",
    "CT": "Ct",
    "PL": "Pl",
    "WAY": "Way",
    "CIR": "Cir",
    "PKWY": "Pkwy",
    "HWY": "Hwy",
    "TER": "Ter",
    "TRL": "Trl",
}

# Census geocoder returns "CITY, ST, ZIP"; other sources use "CITY, ST ZIP".
_STATE_ZIP_TAIL_RE = re.compile(
    r",\s*([^,]+),\s*([A-Z]{2})\s*,?\s*(\d{5})(?:-\d{4})?\s*$",
    re.IGNORECASE,
)
_STATE_ONLY_RE = re.compile(
    r",\s*([A-Z]{2})\s*,?\s*\d{5}(?:-\d{4})?\s*$",
    re.IGNORECASE,
)
# OSM/Nominatim verbose: "... Milwaukee County, Wisconsin, 53225, United States"
_STATE_NAME_ZIP_RE = re.compile(
    r",\s*([A-Za-z][A-Za-z\s\.]+?)\s*,\s*(\d{5})(?:-\d{4})?\s*(?:,\s*United States)?\s*$",
    re.IGNORECASE,
)

_US_STATE_NAME_TO_ABBR: dict[str, str] = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
}


def _is_verbose_geocoder_address(text: str) -> bool:
    """True for Nominatim/OSM verbose forms (comma-heavy, wards, full state names)."""
    return bool(
        is_osm_verbose_address(text)
        or re.search(r"district of columbia|\bward\s+\d+", text, re.IGNORECASE)
    )


def parse_address_components(
    address: str | None,
    *,
    zip_code: str | None = None,
    country: str | None = None,
) -> dict[str, str | None]:
    """Split a formatted address into street, city, state, zip, and country."""
    text = str(address or "").strip()
    if not text:
        return {
            "site_street": None,
            "site_city": None,
            "site_state": None,
            "zip_code": zip_code,
            "site_country": country or "US",
        }

    # Nominatim returns "1201, 4th Street Southeast, Ward 8, Washington, …".
    # Collapse to USPS-style before component split so street/city are usable.
    parse_text = text
    if _is_verbose_geocoder_address(text):
        collapsed = collapse_osm_address(text)
        if collapsed:
            parse_text = collapsed

    resolved_zip = zip_code or parse_zip_from_address(parse_text) or parse_zip_from_address(text)
    city = extract_city_from_address(parse_text) or extract_city_from_address(text)
    state: str | None = None

    tail = _STATE_ZIP_TAIL_RE.search(parse_text) or _STATE_ZIP_TAIL_RE.search(text)
    if tail:
        if not city:
            city = tail.group(1).strip()
        state = tail.group(2).upper()
        if not resolved_zip:
            resolved_zip = tail.group(3)
    else:
        state_match = _STATE_ONLY_RE.search(parse_text) or _STATE_ONLY_RE.search(text)
        if state_match:
            state = state_match.group(1).upper()
        else:
            name_match = _STATE_NAME_ZIP_RE.search(text)
            if name_match:
                state = normalize_state_code(name_match.group(1))
                if not resolved_zip:
                    resolved_zip = name_match.group(2)

    if state is None and re.search(r"\bDC\b|District of Columbia", text, re.IGNORECASE):
        state = "DC"
    if not city and state == "DC" and re.search(r"\bWashington\b", text, re.IGNORECASE):
        city = "Washington"

    if _is_verbose_geocoder_address(text):
        # Prefer street from collapsed USPS form; fall back to POI-stripped head.
        street = extract_street_line_raw(parse_text)
        if not street or re.fullmatch(r"\d+", street or ""):
            working = strip_leading_poi(text)
            working = re.split(
                r",\s*(?:Ward\s+\d+|Washington|District of Columbia|United States)\b",
                working,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            working = re.sub(r"^(\d+)\s*,\s*", r"\1 ", working.strip())
            # Drop standalone region/neighborhood comma parts.
            pieces: list[str] = []
            for part in working.split(","):
                part = part.strip()
                if not part:
                    continue
                if re.fullmatch(
                    r"Northeast|Northwest|Southeast|Southwest|.+\s(?:Neighborhood|Waterfront)",
                    part,
                    flags=re.IGNORECASE,
                ):
                    break
                pieces.append(part)
            street = " ".join(pieces) if pieces else working
    else:
        street = extract_street_line_raw(parse_text)

    return {
        "site_street": format_address_for_upload(street) if street else None,
        "site_city": format_address_for_upload(city) if city else None,
        "site_state": normalize_state_code(state),
        "zip_code": resolved_zip,
        "site_country": country or "US",
    }


def normalize_state_code(value: str | None) -> str | None:
    """Return a 2-letter US state/DC code, or None when unknown."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    upper = _WS_RE.sub(" ", text).upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    return _US_STATE_NAME_TO_ABBR.get(upper)


def format_address_for_upload(text: str | None) -> str | None:
    """Title-case street/city for Salesforce upload while keeping USPS-style tokens."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    words: list[str] = []
    for word in raw.split():
        if word.startswith("#"):
            words.append(word.upper())
            continue
        bare = word.strip(".,;")
        upper = bare.upper()
        if upper in _DIRECTIONALS:
            words.append(upper)
        elif upper in _STREET_SUFFIX_MAP:
            words.append(_STREET_SUFFIX_MAP[upper])
        elif bare.isdigit() or re.match(r"^\d+[A-Z]?$", bare, re.IGNORECASE):
            words.append(bare)
        else:
            words.append(bare.title())
    return _WS_RE.sub(" ", " ".join(words))
