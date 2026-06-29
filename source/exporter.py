"""Export source records for classifier CSVs or orchestrator handoff."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from source.record import SourceRecord


def source_records_from_dataframe(
    df: pd.DataFrame,
    *,
    source_name: str,
    address_col: str = "Address",
    city_col: str = "City",
    state_col: str = "State",
    zip_col: str | None = None,
    county_col: str | None = None,
    country_col: str | None = None,
    id_col: str | None = None,
    lat_col: str | None = None,
    lng_col: str | None = None,
    input_confidence_col: str | None = None,
    label: str | None = None,
    id_prefix: str | None = None,
    metadata_cols: list[str] | None = None,
    source_url: str | None = None,
) -> list[SourceRecord]:
    """Convert a permit dataframe into SourceRecord objects."""
    prefix = (id_prefix or label or source_name).lower().replace(" ", "_")
    records: list[SourceRecord] = []
    reserved = {
        address_col,
        city_col,
        state_col,
        zip_col,
        county_col,
        country_col,
        id_col,
        lat_col,
        lng_col,
        input_confidence_col,
    }
    reserved = {col for col in reserved if col}
    meta_cols = metadata_cols or [c for c in df.columns if c not in reserved]

    for idx, row in df.iterrows():
        address = str(row.get(address_col, "")).strip()
        if not address or address.lower() == "nan":
            continue
        site_num = int(idx) + 1
        site_id = _optional_str(row.get(id_col)) if id_col else None
        input_confidence = (
            _optional_str(row.get(input_confidence_col)) if input_confidence_col else None
        )
        lat = _optional_float(row.get(lat_col)) if lat_col else None
        lng = _optional_float(row.get(lng_col)) if lng_col else None
        records.append(SourceRecord(
            site_id=site_id or f"{prefix}_{site_num:03d}",
            address=address,
            city=_optional_str(row.get(city_col)),
            state=_optional_str(row.get(state_col)),
            county=_optional_str(row.get(county_col)) if county_col else None,
            country=_optional_str(row.get(country_col)) if country_col else None,
            zip_code=_optional_str(row.get(zip_col)) if zip_col else None,
            lat=lat,
            lng=lng,
            label=label or _optional_str(row.get("label")) or _optional_str(row.get(state_col)),
            input_confidence=input_confidence or "high",
            permit_metadata={
                col: _clean_value(row.get(col))
                for col in meta_cols
                if _clean_value(row.get(col)) not in (None, "")
            },
            source_name=source_name,
            source_url=source_url,
        ))
    return records


def export_assets_csv(
    records: list[SourceRecord],
    path: str | Path,
    *,
    label: str | None = None,
    input_confidence: str = "high",
) -> Path:
    """Write classifier-ready assets CSV (id, address, label, input_confidence)."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for idx, record in enumerate(records, start=1):
        prefix = (record.label or label or "site").lower().replace(" ", "_")
        site_id = record.site_id or f"{prefix}_{idx:03d}"
        rows.append({
            "id": site_id,
            "address": record.full_address,
            "label": record.label or label or "",
            "input_confidence": record.input_confidence or input_confidence,
        })
    pd.DataFrame(rows).to_csv(output, index=False)
    return output


def export_source_json(records: list[SourceRecord], path: str | Path) -> Path:
    """Write source records as JSON for Claude/manual review workflows."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "site_id": r.site_id,
            "address": r.full_address,
            "city": r.city,
            "state": r.state,
            "county": r.county,
            "country": r.country,
            "zip_code": r.zip_code,
            "lat": r.lat,
            "lng": r.lng,
            "label": r.label,
            "input_confidence": r.input_confidence,
            "source_name": r.source_name,
            "source_url": r.source_url,
            "permit_metadata": r.permit_metadata,
        }
        for r in records
    ]
    pd.DataFrame(payload).to_json(output, orient="records", indent=2)
    return output


def _optional_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _clean_value(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str) and value.strip().lower() == "nan":
        return None
    return value
