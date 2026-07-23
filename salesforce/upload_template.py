"""Salesforce bulk-upload template — columns, picklists, and row builders."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ingest.address_parts import (
    format_address_for_upload,
    normalize_state_code,
    parse_address_components,
)

# CSV headers match the manual Data Loader template (required fields marked * in docs).
UPLOAD_CSV_COLUMNS: list[str] = [
    "Site Street",
    "Site City",
    "Site State",
    "Site Zip Code",
    "Site Country",
    "Site Latitude",
    "Site Longitude",
    "Carrier Leasing Source",
    "OwnerId",
    "Site Type",
    "Verified Site",
    "Verified Site Source",
    "Morphology",
    # "Property Type",  # omitted for now — no vision/permit source yet
]

REQUIRED_UPLOAD_COLUMNS: frozenset[str] = frozenset(
    {
        "Site Street",
        "Site City",
        "Site State",
        "Site Zip Code",
        "Site Country",
        "Site Latitude",
        "Site Longitude",
        "Carrier Leasing Source",
        "OwnerId",
    }
)

DEFAULT_OWNER_ID = "0056O00000EpUOgQAN"

SITE_TYPE_VALUES: tuple[str, ...] = (
    "Billboard",
    "DAS",
    "Datacenter",
    "Equipment Only",
    "Flagpole",
    "Guyed Tower",
    "Monopole",
    "Mountain",
    "Rooftop",
    "Self Support / Lattice Tower",
    "Silo",
    "Small Cell",
    "Smokestack",
    "Stealth",
    "Steeple",
    "Water Tower",
    "Tower Land",
)

VERIFIED_SITE_SOURCE_VALUES: tuple[str, ...] = (
    "AT&T",
    "Beenverified",
    "Bing Map",
    "Dish",
    "FCC",
    "Google Map",
    "In Person Verified",
    "Permitting Data",
    "ReGrid",
    "Scadacore",
    "T-Mobile",
    "Verbal Confirmation",
    "Verizon",
    "Zillow",
    "Synaptek",
    "Intern Verified",
)

MORPHOLOGY_VALUES: tuple[str, ...] = (
    "Rural",
    "Suburban",
    "Urban",
    "Dense Urban",
)

# PROPERTY_TYPE_VALUES: tuple[str, ...] = (
#     "Commercial",
#     "Education",
#     "HealthCare/ Life Sciences",
#     "Hotel / Hospitality",
#     "Industrial",
#     "Mixed - Use",
#     "MultiFamily",
#     "Municipal",
#     "Office",
#     "Other",
#     "Religious",
#     "Single Family Home",
#     "Storage Facility",
#     "Raw Land",
# )

from salesforce.site_type_mapping import map_site_type_for_upload

URBANICITY_MORPHOLOGY_MAP: dict[str, str] = {
    "rural": "Rural",
    "suburban": "Suburban",
    "urban": "Urban",
}


def permit_scraping_carrier_leasing_source(when: datetime | None = None) -> str:
    """Return Carrier_Leasing_Source__c value: JF_PermitScraping_{mon}{yy} (e.g. jul26)."""
    configured = os.environ.get("SF_CARRIER_LEASING_SOURCE", "").strip()
    if configured:
        return configured
    moment = when or datetime.now()
    month_abbr = moment.strftime("%b").lower()
    year = moment.strftime("%y")
    return f"JF_PermitScraping_{month_abbr}{year}"


def default_carrier_leasing_source(when: datetime | None = None) -> str:
    return permit_scraping_carrier_leasing_source(when)


def default_owner_id() -> str:
    configured = os.environ.get("SF_OWNER_ID", "").strip()
    if configured:
        return configured
    return DEFAULT_OWNER_ID


def default_verified_site_source() -> str:
    configured = os.environ.get("SF_DEFAULT_VERIFIED_SOURCE", "").strip()
    if configured:
        return configured
    return "Permitting Data"


# def default_property_type() -> str:
#     return os.environ.get("SF_DEFAULT_PROPERTY_TYPE", "Commercial").strip() or "Commercial"


def map_classifier_site_type(
    classifier_type: str | None,
    *,
    tower_subtype: str | None = None,
    cell_equipment: Any = None,
    permit_metadata: dict[str, Any] | None = None,
) -> str:
    return map_site_type_for_upload(
        {
            "site_type": classifier_type,
            "tower_subtype": tower_subtype,
            "cell_equipment": cell_equipment,
        },
        permit_metadata=permit_metadata,
    )


def map_morphology(
    *,
    urbanicity_tier: str | None = None,
    zip_population: int | None = None,
) -> str:
    tier = (urbanicity_tier or "").lower()
    if tier == "urban" and zip_population is not None and zip_population >= 50_000:
        return "Dense Urban"
    return URBANICITY_MORPHOLOGY_MAP.get(tier, "Suburban")


def validate_picklist(field: str, value: str | None) -> list[str]:
    """Return validation errors for a picklist value (empty is allowed)."""
    if value is None or str(value).strip() == "":
        return []
    text = str(value).strip()
    allowed: tuple[str, ...] | None = None
    if field in {"Site Type", "site_type"}:
        allowed = SITE_TYPE_VALUES
    elif field in {"Verified Site Source", "verified_site_source"}:
        allowed = VERIFIED_SITE_SOURCE_VALUES
    elif field in {"Morphology", "morphology"}:
        allowed = MORPHOLOGY_VALUES
    # elif field in {"Property Type", "property_type"}:
    #     allowed = PROPERTY_TYPE_VALUES
    elif field in {"Verified Site", "verified_site"}:
        if text.upper() not in {"TRUE", "FALSE"}:
            return [f"{field} must be TRUE or FALSE, got {text!r}"]
        return []
    if allowed is not None and text not in allowed:
        return [f"{field} value {text!r} is not in the Salesforce picklist"]
    return []


def build_upload_record(
    canonical: dict[str, Any],
    *,
    classified: dict[str, Any] | None = None,
    dedupe_row: dict[str, Any] | None = None,
    carrier_leasing_source: str | None = None,
    owner_id: str | None = None,
    upload_when: datetime | None = None,
    verified_site: bool | None = None,
    verified_site_source: str | None = None,
    # property_type: str | None = None,
    site_type: str | None = None,
    morphology: str | None = None,
) -> dict[str, Any]:
    """Build a canonical upload dict from pipeline records."""
    classified = classified or {}
    dedupe_row = dedupe_row or {}
    meta = canonical.get("permit_metadata") or {}
    parts = parse_address_components(
        canonical.get("address"),
        zip_code=canonical.get("zip_code") or dedupe_row.get("zip_code"),
    )
    if not parts.get("site_state"):
        parts["site_state"] = normalize_state_code(
            canonical.get("state")
            or meta.get("scope_state")
            or meta.get("state")
            or dedupe_row.get("state")
        )

    resolved_site_type = site_type
    if not resolved_site_type:
        resolved_site_type = map_classifier_site_type(
            classified.get("site_type"),
            tower_subtype=classified.get("tower_subtype"),
            cell_equipment=classified.get("cell_equipment"),
            permit_metadata=canonical.get("permit_metadata"),
        )

    resolved_morphology = morphology
    if not resolved_morphology:
        resolved_morphology = map_morphology(
            urbanicity_tier=dedupe_row.get("urbanicity_tier"),
            zip_population=_coerce_int(dedupe_row.get("zip_population")),
        )

    verified = "TRUE" if (verified_site if verified_site is not None else True) else "FALSE"
    source = verified_site_source or default_verified_site_source()
    # prop_type = property_type or default_property_type()
    carrier = carrier_leasing_source or default_carrier_leasing_source(upload_when)
    resolved_owner_id = owner_id or default_owner_id()

    lat = canonical.get("lat")
    lng = canonical.get("lng")
    full_address = canonical.get("address") or _compose_address(parts)

    record: dict[str, Any] = {
        **parts,
        "address": full_address,
        "lat": lat,
        "lng": lng,
        "carrier_leasing_source": carrier,
        "owner_id": resolved_owner_id,
        "site_type": resolved_site_type,
        "verified_site": verified,
        "verified_site_source": source,
        "morphology": resolved_morphology,
        # "property_type": prop_type,
        "site_confidence": classified.get("site_confidence"),
        "cell_equipment": classified.get("cell_equipment"),
        "permit_metadata": canonical.get("permit_metadata") or {},
        "source_url": canonical.get("source_url") or classified.get("source_url"),
    }
    return record


def upload_record_to_csv_row(record: dict[str, Any]) -> dict[str, str]:
    """Map a canonical upload dict to template CSV column names."""
    return {
        "Site Street": _csv_text(record.get("site_street")),
        "Site City": _csv_text(record.get("site_city")),
        "Site State": _csv_text(record.get("site_state")),
        "Site Zip Code": _csv_text(record.get("zip_code")),
        "Site Country": _csv_text(record.get("site_country") or "US"),
        "Site Latitude": _csv_number(record.get("lat"), precision=5),
        "Site Longitude": _csv_number(record.get("lng"), precision=5),
        "Carrier Leasing Source": _csv_text(record.get("carrier_leasing_source")),
        "OwnerId": _csv_text(record.get("owner_id") or default_owner_id()),
        "Site Type": _csv_text(record.get("site_type")),
        "Verified Site": _csv_text(record.get("verified_site") or "TRUE"),
        "Verified Site Source": _csv_text(record.get("verified_site_source")),
        "Morphology": _csv_text(record.get("morphology")),
        # "Property Type": _csv_text(record.get("property_type")),
    }


def csv_row_to_upload_record(row: dict[str, Any]) -> dict[str, Any]:
    """Map a Salesforce upload template CSV row back to a canonical upload dict."""
    street = format_address_for_upload(_csv_text(row.get("Site Street"))) or ""
    city = format_address_for_upload(_csv_text(row.get("Site City"))) or ""
    state = normalize_state_code(_csv_text(row.get("Site State"))) or ""
    zip_code = _csv_text(row.get("Site Zip Code"))
    country = _csv_text(row.get("Site Country") or "US")
    lat = row.get("Site Latitude")
    lng = row.get("Site Longitude")
    address = _compose_address(
        {
            "site_street": street,
            "site_city": city,
            "site_state": state,
            "zip_code": zip_code,
        }
    )
    return {
        "site_street": street,
        "site_city": city,
        "site_state": state,
        "zip_code": zip_code,
        "site_country": country,
        "address": address,
        "lat": float(lat) if lat not in (None, "") else None,
        "lng": float(lng) if lng not in (None, "") else None,
        "carrier_leasing_source": _csv_text(row.get("Carrier Leasing Source")),
        "owner_id": _csv_text(row.get("OwnerId") or default_owner_id()),
        "site_type": _csv_text(row.get("Site Type")),
        "verified_site": _csv_text(row.get("Verified Site") or "TRUE"),
        "verified_site_source": _csv_text(row.get("Verified Site Source")),
        "morphology": _csv_text(row.get("Morphology")),
    }


def validate_upload_record(record: dict[str, Any]) -> list[str]:
    """Validate required fields and picklist values for one upload row."""
    csv_row = upload_record_to_csv_row(record)
    errors: list[str] = []
    for column in REQUIRED_UPLOAD_COLUMNS:
        if not csv_row.get(column, "").strip():
            errors.append(f"Missing required field: {column}")
    for column in UPLOAD_CSV_COLUMNS:
        errors.extend(validate_picklist(column, csv_row.get(column)))
    return errors


def write_upload_csv(
    records: list[dict[str, Any]],
    output_path: Path | str,
    *,
    include_picklist_reference: bool = True,
) -> Path:
    """Write net-new site rows to the Salesforce upload CSV template."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [upload_record_to_csv_row(record) for record in records]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=UPLOAD_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    if include_picklist_reference:
        _write_picklist_reference(path.with_name(f"{path.stem}_picklists.txt"))
    return path


def _write_picklist_reference(path: Path) -> None:
    sections = [
        ("Site Type", SITE_TYPE_VALUES),
        ("Verified Site Source", VERIFIED_SITE_SOURCE_VALUES),
        ("Morphology", MORPHOLOGY_VALUES),
        # ("Property Type", PROPERTY_TYPE_VALUES),
        ("Verified Site", ("TRUE", "FALSE")),
    ]
    lines = ["Salesforce upload picklist reference", ""]
    for title, values in sections:
        lines.append(f"{title}:")
        lines.extend(f"  - {value}" for value in values)
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _compose_address(parts: dict[str, str | None]) -> str:
    chunks = [
        parts.get("site_street"),
        parts.get("site_city"),
        parts.get("site_state"),
        parts.get("zip_code"),
    ]
    return ", ".join(str(chunk) for chunk in chunks if chunk)


def _csv_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _csv_number(value: Any, *, precision: int) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value):.{precision}f}"


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
