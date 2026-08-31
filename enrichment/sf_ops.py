"""Salesforce query + update helpers for enrichment."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Sequence

from salesforce.field_map import OBJECT_NAME
from salesforce.sf_client import SalesforceClient

from enrichment.constants import (
    DEFAULT_OWNER_FILTER,
    DEFAULT_STAGE_FILTER,
    EXCLUDED_STAGE_FILTER,
    SF_QUERY_FIELDS,
)

logger = logging.getLogger(__name__)

# Site fields written for tower/rooftop enrichment (vs LLM_Classified__c-only updates).
ENRICHMENT_FIELDS = frozenset(
    {
        "Site_Latitude__c",
        "Site_Longitude__c",
        "Site_Type__c",
        "Verified_Site__c",
        "Verified_Site_Source__c",
    }
)


def is_enrichment_payload(payload: dict[str, Any] | None) -> bool:
    """True when the payload updates tower/site fields, not only LLM_Classified__c."""
    if not payload:
        return False
    return bool(ENRICHMENT_FIELDS.intersection(payload))


def parse_carrier_like(raw: str | None, *, default: str | None = None) -> str | None:
    """Carrier_Leasing_Source__c LIKE needle from env/CLI.

    Unset/blank → no carrier filter. Set ``CARRIER_LIKE=NFL`` (or any needle)
    to add LIKE '%value%'. ``none`` / ``all`` / ``*`` also omit the filter.
    """
    return _parse_optional_filter(raw, default=default)


def parse_metro_classification(
    raw: str | None, *, default: str = "Major NFL Metro"
) -> str | None:
    """Metro_Classification__c exact value from env/CLI.

    Unset/blank → ``default`` (Major NFL Metro). ``none`` / ``all`` / ``*``
    omit the filter.
    """
    return _parse_optional_filter(raw, default=default)


def _parse_optional_filter(raw: str | None, *, default: str | None) -> str | None:
    text = "" if raw is None else str(raw).strip()
    if not text:
        if default is None or not str(default).strip():
            return None
        text = str(default).strip()
    if text.lower() in {"none", "all", "*"}:
        return None
    return text


def _soql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _soql_in(values: Sequence[str]) -> str:
    return ", ".join(_soql_quote(v) for v in values)


def build_blank_site_type_query(
    *,
    stages: Sequence[str] = DEFAULT_STAGE_FILTER,
    owners: Sequence[str] = DEFAULT_OWNER_FILTER,
    fields: Sequence[str] = SF_QUERY_FIELDS,
    carrier_like: str | None = None,
    metro_classification: str | None = "Major NFL Metro",
    states: Sequence[str] | None = None,
    llm_classified: bool = False,
) -> str:
    """SOQL for blank Site_Type__c sites in the enrichment queue.

    `carrier_like` filters Carrier_Leasing_Source__c with LIKE '%value%'.
    Pass None/"" to skip the carrier filter (the default).
    `metro_classification` filters Metro_Classification__c with an exact
    match (default Major NFL Metro). Pass None/"" to skip.
    `states` filters Site_State__c IN (...); pass None/empty for all states.
    `llm_classified` defaults False (sites not yet LLM-classified). True selects
    the already-flagged NFL re-queue.
    """
    field_list = ", ".join(fields)
    classified_sql = "true" if llm_classified else "false"
    clauses = [
        "(Site_Type__c = null OR Site_Type__c = '')",
        f"LLM_Classified__c = {classified_sql}",
        "(LLM_Holdout__c = false OR LLM_Holdout__c = null)",
        "Site_Latitude__c != null AND Site_Latitude__c != ''",
        "Site_Longitude__c != null AND Site_Longitude__c != ''",
        f"Stage__c IN ({_soql_in(stages)})",
        f"Stage__c NOT IN ({_soql_in(EXCLUDED_STAGE_FILTER)})",
        f"Owner__c IN ({_soql_in(owners)})",
    ]
    carrier = (carrier_like or "").strip()
    if carrier:
        escaped = carrier.replace("\\", "\\\\").replace("'", "\\'")
        clauses.insert(1, f"Carrier_Leasing_Source__c LIKE '%{escaped}%'")
    metro = (metro_classification or "").strip()
    if metro:
        clauses.insert(1, f"Metro_Classification__c = {_soql_quote(metro)}")
    clean_states = [
        str(s).strip().upper() for s in (states or []) if str(s).strip()
    ]
    if clean_states:
        clauses.append(f"Site_State__c IN ({_soql_in(clean_states)})")
    return f"SELECT {field_list} FROM {OBJECT_NAME} WHERE " + " AND ".join(clauses)


def build_sites_by_ids_query(
    ids: Sequence[str],
    *,
    fields: Sequence[str] = SF_QUERY_FIELDS,
) -> str:
    """SOQL for an explicit Site__c Id list (controlled test batches)."""
    clean = [str(i).strip() for i in ids if str(i).strip()]
    if not clean:
        raise ValueError("build_sites_by_ids_query requires at least one Id")
    field_list = ", ".join(fields)
    return (
        f"SELECT {field_list} FROM {OBJECT_NAME} "
        f"WHERE Id IN ({_soql_in(clean)})"
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
    carrier_like: str | None = None,
    metro_classification: str | None = "Major NFL Metro",
    states: Sequence[str] | None = None,
    llm_classified: bool = False,
) -> list[dict[str, Any]]:
    soql = build_blank_site_type_query(
        stages=stages,
        owners=owners,
        carrier_like=carrier_like,
        metro_classification=metro_classification,
        states=states,
        llm_classified=llm_classified,
    )
    logger.info("Salesforce SOQL: %s", soql)
    return query_all(client, soql)


def query_sites_by_ids(
    client: SalesforceClient,
    ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Fetch Site__c rows by Id, preserving the requested order."""
    soql = build_sites_by_ids_query(ids)
    logger.info("Salesforce SOQL: %s", soql)
    rows = query_all(client, soql)
    by_id = {str(r.get("Id") or ""): r for r in rows}
    ordered: list[dict[str, Any]] = []
    for sid in ids:
        key = str(sid).strip()
        if key in by_id:
            ordered.append(by_id[key])
    return ordered


def parse_sf_lat_lng(row: dict[str, Any]) -> tuple[float, float] | None:
    lat_raw = row.get("Site_Latitude__c")
    lng_raw = row.get("Site_Longitude__c")
    if _is_missing(lat_raw) or _is_missing(lng_raw):
        return None
    try:
        return float(lat_raw), float(lng_raw)
    except (TypeError, ValueError):
        return None


# Fallback when Site_Duplicate_Rule blocks enrichment field writes.
# LLM_Classified=false removes the site from the blank-Site_Type enrichment queue.
# LLM_Holdout=true marks that enrichment could not write site fields.
DUPLICATE_FALLBACK_PAYLOAD = {
    "LLM_Classified__c": False,
    "LLM_Holdout__c": True,
    "Test_Batch_Flag__c": True,
}


def build_update_payload(
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    site_type: str | None = None,
    verified_site: bool | None = None,
    verified_site_source: str | None = None,
    llm_classified: bool | None = None,
    llm_holdout: bool | None = None,
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
    if llm_classified is not None:
        payload["LLM_Classified__c"] = llm_classified
    if llm_holdout is not None:
        payload["LLM_Holdout__c"] = llm_holdout
    if test_batch_flag is not None:
        payload["Test_Batch_Flag__c"] = test_batch_flag
    return payload


def apply_queue_flags(payload: dict[str, Any]) -> dict[str, Any]:
    """Set LLM_Classified / LLM_Holdout from whether site enrichment fields are present.

    Successful enrichment: LLM_Classified=true, LLM_Holdout=false.
    Holdout / dequeue-only: LLM_Classified=false, LLM_Holdout=true.
    """
    out = dict(payload)
    is_enrich = is_enrichment_payload(out)
    out["LLM_Classified__c"] = is_enrich
    out["LLM_Holdout__c"] = not is_enrich
    return out


def _is_duplicates_detected(exc: BaseException) -> bool:
    """True when Salesforce rejected the update for Site_Duplicate_Rule."""
    return "DUPLICATES_DETECTED" in str(exc)


def _is_duplicate_fallback_payload(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    return set(payload) == set(DUPLICATE_FALLBACK_PAYLOAD) and all(
        payload.get(key) == value for key, value in DUPLICATE_FALLBACK_PAYLOAD.items()
    )


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
    """Update a single enrichment candidate row; never raises.

    Eligible tower/rooftop writes: LLM_Classified__c=true, LLM_Holdout__c=false.
    Holdouts (no site fields): LLM_Classified__c=false, LLM_Holdout__c=true so
    they leave the blank-Site_Type enrichment queue and are flagged as holdouts.

    On DUPLICATES_DETECTED for an enrichment payload, retries once with
    LLM_Classified__c=false + LLM_Holdout__c=true + Test_Batch_Flag__c so the
    site still dequeues.
    """
    from enrichment import progress

    sf_id = str(row.get("Id") or row.get("sf_id") or "").strip()
    payload = row.get("payload")
    if not isinstance(payload, dict):
        site_fields = build_update_payload(
            latitude=_optional_float(row.get("update_lat")),
            longitude=_optional_float(row.get("update_lng")),
            site_type=(row.get("update_site_type") or None) or None,
            verified_site=_optional_bool(row.get("update_verified_site")),
            verified_site_source=(row.get("update_verified_site_source") or None)
            or None,
        )
        payload = apply_queue_flags(site_fields)
    else:
        # Preserve explicit queue flags on prebuilt payloads; fill any gaps.
        payload = dict(payload)
        is_enrich = is_enrichment_payload(payload)
        if "LLM_Classified__c" not in payload:
            payload["LLM_Classified__c"] = is_enrich
        if "LLM_Holdout__c" not in payload:
            payload["LLM_Holdout__c"] = not is_enrich
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
        has_enrichment = is_enrichment_payload(payload)
        allowed_types = {"tower", "rooftop"}
        if has_enrichment and naip_site_type not in allowed_types:
            raise ValueError(
                f"Salesforce updates require NAIP site_type=tower|rooftop; got "
                f"{naip_site_type or 'blank'}"
            )
        if not payload:
            raise ValueError("Empty update payload")
        if dry_run:
            entry["success"] = True
            entry["status"] = "dry_run"
            if verbose:
                progress.result(_format_apply_result(payload, dry_run=True))
            return entry
        update_site(client, sf_id, payload, verbose=False)
        entry["success"] = True
        entry["status"] = "updated"
        if verbose:
            progress.result(_format_apply_result(payload, dry_run=False))
    except Exception as exc:  # noqa: BLE001 — per-row resilience
        if (
            not dry_run
            and sf_id
            and _is_duplicates_detected(exc)
            and is_enrichment_payload(payload)
            and not _is_duplicate_fallback_payload(payload)
        ):
            fallback = dict(DUPLICATE_FALLBACK_PAYLOAD)
            try:
                update_site(client, sf_id, fallback, verbose=False)
                entry["success"] = True
                entry["status"] = "updated_llm_after_duplicate"
                entry["payload"] = fallback
                entry["error"] = f"fallback after DUPLICATES_DETECTED: {exc}"
                logger.warning(
                    "enrichment blocked by duplicate for %s — dequeued LLM/Holdout/Test_Batch only",
                    sf_id,
                )
                if verbose:
                    progress.warn(
                        "SF duplicate blocked enrichment — "
                        "dequeued (LLM_Classified=false, LLM_Holdout=true, Test_Batch_Flag)"
                    )
                return entry
            except Exception as fallback_exc:  # noqa: BLE001
                entry["error"] = (
                    f"DUPLICATES_DETECTED fallback also failed: {fallback_exc} "
                    f"(original: {exc})"
                )
                entry["status"] = "failed"
                entry["payload"] = fallback
                logger.warning(
                    "duplicate fallback failed for %s: %s", sf_id, fallback_exc
                )
                if verbose:
                    progress.warn(
                        f"SF update failed — continuing: {entry['error']}"
                    )
                return entry

        entry["error"] = str(exc)
        entry["status"] = "failed"
        logger.warning("update failed for %s: %s", sf_id, exc)
        if verbose:
            progress.warn(f"SF update failed — continuing: {exc}")
    return entry


def _format_apply_result(
    payload: dict[str, Any],
    *,
    dry_run: bool,
    status: str = "",
) -> str:
    """Human-readable apply line: enrichment details, or holdout dequeue."""
    prefix = "SF dry-run OK (not written)" if dry_run else "SF updated"
    if status == "updated_llm_after_duplicate":
        return (
            f"{prefix} | duplicate fallback | "
            "dequeued (LLM_Classified=false, LLM_Holdout=true, Test_Batch_Flag)"
        )
    if not is_enrichment_payload(payload):
        return (
            f"{prefix} | dequeued "
            "(LLM_Classified=false, LLM_Holdout=true, no site fields written)"
        )

    site_type = payload.get("Site_Type__c") or "—"
    verified = payload.get("Verified_Site_Source__c") or "—"
    lat = payload.get("Site_Latitude__c")
    lng = payload.get("Site_Longitude__c")
    coords = f"{lat}, {lng}" if lat is not None and lng is not None else "coords unchanged"
    return f"{prefix} | verified={verified} | type={site_type} | {coords}"


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
        sf_id = str(row.get("Id") or row.get("sf_id") or "").strip()
        address = progress.format_site_address(row)
        if verbose:
            progress.step(f"[{index}/{len(rows_list)}] Id={sf_id or '—'}")
        else:
            progress.row_count(
                index, len(rows_list), sf_id=sf_id, address=address
            )
        row_t0 = time.monotonic()
        entry = apply_one_update(client, row, dry_run=dry_run, verbose=False)
        row_elapsed = time.monotonic() - row_t0
        entry["index"] = index
        if verbose:
            if entry.get("success"):
                progress.result(
                    _format_apply_result(
                        entry.get("payload") or {},
                        dry_run=dry_run,
                        status=str(entry.get("status") or ""),
                    ),
                    elapsed_s=row_elapsed,
                )
            else:
                progress.warn(
                    f"SF update failed — continuing: {entry.get('error') or 'unknown'}",
                    elapsed_s=row_elapsed,
                )
        results.append(entry)
    return results


def _is_missing(value: Any) -> bool:
    """True for None/blank/NaN — never send these in a Salesforce JSON payload."""
    if value is None:
        return True
    if isinstance(value, float):
        try:
            return value != value  # NaN
        except Exception:
            return False
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, str) and value.strip().lower() == "nan":
        return True
    return False


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
