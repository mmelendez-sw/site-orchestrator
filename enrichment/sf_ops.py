"""Salesforce query + update helpers for enrichment (additive; create path untouched)."""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from salesforce.field_map import OBJECT_NAME
from salesforce.sf_client import SalesforceClient, _is_missing

from enrichment.constants import (
    DEFAULT_OWNER_FILTER,
    DEFAULT_STAGE_FILTER,
    SF_QUERY_FIELDS,
)

logger = logging.getLogger(__name__)


def _soql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _soql_in(values: Sequence[str]) -> str:
    return ", ".join(_soql_quote(v) for v in values)


def build_blank_site_type_query(
    *,
    stages: Sequence[str] = DEFAULT_STAGE_FILTER,
    owners: Sequence[str] = DEFAULT_OWNER_FILTER,
    fields: Sequence[str] = SF_QUERY_FIELDS,
) -> str:
    """SOQL for Site__c rows with blank/null Site_Type__c in the target queue."""
    field_list = ", ".join(fields)
    return (
        f"SELECT {field_list} FROM {OBJECT_NAME} "
        f"WHERE (Site_Type__c = null OR Site_Type__c = '') "
        f"AND Site_Latitude__c != null AND Site_Latitude__c != '' "
        f"AND Site_Longitude__c != null AND Site_Longitude__c != '' "
        f"AND Stage__c IN ({_soql_in(stages)}) "
        f"AND Owner__c IN ({_soql_in(owners)})"
    )


def query_all(client: SalesforceClient, soql: str) -> list[dict[str, Any]]:
    """Run SOQL and follow nextRecordsUrl until exhausted."""
    result = client.sf.query(soql)
    records = list(result.get("records") or [])
    while not result.get("done", True) and result.get("nextRecordsUrl"):
        result = client.sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
        records.extend(result.get("records") or [])
    # Strip Salesforce attribute metadata.
    cleaned: list[dict[str, Any]] = []
    for row in records:
        cleaned.append({k: v for k, v in row.items() if k != "attributes"})
    return cleaned


def query_blank_site_type_sites(
    client: SalesforceClient,
    *,
    stages: Sequence[str] = DEFAULT_STAGE_FILTER,
    owners: Sequence[str] = DEFAULT_OWNER_FILTER,
) -> list[dict[str, Any]]:
    soql = build_blank_site_type_query(stages=stages, owners=owners)
    logger.info("Salesforce SOQL: %s", soql)
    return query_all(client, soql)


def parse_sf_lat_lng(row: dict[str, Any]) -> tuple[float, float] | None:
    lat_raw = row.get("Site_Latitude__c")
    lng_raw = row.get("Site_Longitude__c")
    if _is_missing(lat_raw) or _is_missing(lng_raw):
        return None
    try:
        return float(lat_raw), float(lng_raw)
    except (TypeError, ValueError):
        return None


def build_update_payload(
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    site_type: str | None = None,
    verified_site: bool | None = None,
    verified_site_source: str | None = None,
    test_batch_flag: bool | None = None,
) -> dict[str, Any]:
    """Build a Site__c update payload (only set fields)."""
    payload: dict[str, Any] = {}
    if latitude is not None:
        payload["Site_Latitude__c"] = latitude
    if longitude is not None:
        payload["Site_Longitude__c"] = longitude
    if site_type:
        payload["Site_Type__c"] = site_type
    if verified_site is not None:
        payload["Verified_Site__c"] = verified_site
    if verified_site_source:
        payload["Verified_Site_Source__c"] = verified_site_source
    if test_batch_flag is not None:
        payload["Test_Batch_Flag__c"] = test_batch_flag
    return payload


def update_site(
    client: SalesforceClient,
    record_id: str,
    payload: dict[str, Any],
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """Update one Site__c row. Raises on API failure; caller should catch per-row."""
    if not record_id:
        raise ValueError("Salesforce Id is required for update")
    if not payload:
        raise ValueError("Update payload is empty")
    if verbose:
        logger.info("SF update %s payload=%s", record_id, payload)
    result = getattr(client.sf, OBJECT_NAME).update(record_id, payload)
    # simple_salesforce returns HTTP status int on success for update.
    return {"id": record_id, "status": result, "success": True}


def apply_one_update(
    client: SalesforceClient,
    row: dict[str, Any],
    *,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """Update a single enrichment candidate row; never raises."""
    from enrichment import progress

    sf_id = str(row.get("Id") or row.get("sf_id") or "").strip()
    match_source = str(row.get("match_source") or "").strip()
    verified_source = str(row.get("update_verified_site_source") or "").strip()
    hit_source = match_source if match_source and match_source != "none" else verified_source
    hit_source = hit_source or "unknown"
    payload = row.get("payload")
    if not isinstance(payload, dict):
        payload = build_update_payload(
            latitude=_optional_float(row.get("update_lat")),
            longitude=_optional_float(row.get("update_lng")),
            site_type=(row.get("update_site_type") or None) or None,
            verified_site=_optional_bool(row.get("update_verified_site")),
            verified_site_source=(row.get("update_verified_site_source") or None) or None,
            test_batch_flag=True,
        )
    entry = {
        "Id": sf_id,
        "success": False,
        "dry_run": dry_run,
        "error": "",
        "status": "",
        "payload": payload,
    }
    try:
        if not sf_id:
            raise ValueError("Missing Salesforce Id")
        naip_site_type = str(row.get("naip_site_type") or "").strip().lower()
        enrichment_fields = {
            "Site_Latitude__c",
            "Site_Longitude__c",
            "Site_Type__c",
            "Verified_Site__c",
            "Verified_Site_Source__c",
        }
        allowed_types = {"tower", "rooftop"}
        if enrichment_fields.intersection(payload) and naip_site_type not in allowed_types:
            raise ValueError(
                f"Salesforce updates require NAIP site_type=tower or rooftop; got "
                f"{naip_site_type or 'blank'}"
            )
        if not payload:
            raise ValueError("Empty update payload")
        if dry_run:
            entry["success"] = True
            entry["status"] = "dry_run"
            if verbose:
                progress.result(f"SF dry-run OK (not written) | source={hit_source}")
            return entry
        update_site(client, sf_id, payload, verbose=False)
        entry["success"] = True
        entry["status"] = "updated"
        if verbose:
            progress.result(
                f"SF updated | source={hit_source} | "
                f"type={payload.get('Site_Type__c') or '—'} | "
                f"{payload.get('Site_Latitude__c')}, {payload.get('Site_Longitude__c')}"
            )
    except Exception as exc:  # noqa: BLE001 — per-row resilience
        entry["error"] = str(exc)
        entry["status"] = "failed"
        logger.warning("update failed for %s: %s", sf_id, exc)
        if verbose:
            progress.warn(f"SF update failed — continuing: {exc}")
    return entry


def apply_updates_idempotent(
    client: SalesforceClient,
    rows: Iterable[dict[str, Any]],
    *,
    dry_run: bool = True,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """Apply updates one row at a time; failures are logged and skipped."""
    from enrichment import progress

    rows_list = list(rows)
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows_list, start=1):
        if verbose:
            progress.step(f"[{index}/{len(rows_list)}] Id={row.get('Id') or '—'}")
        entry = apply_one_update(client, row, dry_run=dry_run, verbose=verbose)
        entry["index"] = index
        results.append(entry)
    return results


def _optional_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None
