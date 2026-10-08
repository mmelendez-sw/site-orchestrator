"""Salesforce query + update helpers for enrichment."""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from salesforce.field_map import OBJECT_NAME
from salesforce.sf_client import SalesforceClient

from enrichment.coerce import is_missing, to_bool, to_float
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


def parse_owners(
    raw: str | None,
    *,
    default: Sequence[str] | None = DEFAULT_OWNER_FILTER,
) -> list[str] | None:
    """Owner__c IN-list from env/CLI.

    Unset/blank → ``default``. ``none`` / ``all`` / ``*`` omit the owner IN-list.
    Comma-separated picklist values otherwise. Use ``OWNERS_EXCLUDE`` for
    ``Owner__c NOT IN``.
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        if default is None:
            return None
        return [str(v).strip() for v in default if str(v).strip()]
    if text.lower() in {"none", "all", "*"}:
        return None
    return [part.strip() for part in text.split(",") if part.strip()] or None


def parse_site_type(raw: str | None, *, default: str | None = None) -> str | None:
    """Site_Type__c filter from env/CLI.

    Unset/blank → ``default`` (None = blank Site_Type only).
    ``none`` / ``any`` / ``all`` / ``*`` omit the Site_Type filter.
    Any other value is an exact picklist match (e.g. Rooftop).
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return default
    if text.lower() in {"none", "any", "all", "*"}:
        return "any"
    return text


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
    owners: Sequence[str] | None = DEFAULT_OWNER_FILTER,
    exclude_owners: Sequence[str] | None = None,
    fields: Sequence[str] = SF_QUERY_FIELDS,
    carrier_like: str | None = None,
    metro_classification: str | None = "Major NFL Metro",
    states: Sequence[str] | None = None,
    llm_classified: bool = False,
    site_type: str | None = None,
) -> str:
    """SOQL for enrichment queue sites.

    Default: blank ``Site_Type__c``. Pass ``site_type='Rooftop'`` to audit
    rows that already have that picklist value. Pass ``any`` / ``all`` / ``*``
    to omit the Site_Type filter (Nearmap rooftop confirm).

    `carrier_like` filters Carrier_Leasing_Source__c with LIKE '%value%'.
    Pass None/"" to skip the carrier filter (the default).
    `metro_classification` filters Metro_Classification__c with an exact
    match, or IN (...) for a comma list. Pass None/"" to skip.
    `owners` None/empty omits the Owner__c IN-list (any owner).
    `exclude_owners` adds Owner__c NOT IN (...) and still includes blank owner.
    `states` filters Site_State__c IN (...); pass None/empty for all states.
    `llm_classified` defaults False (sites not yet LLM-classified). True selects
    the already-flagged NFL re-queue.
    `Working-Connected` / `Qualified (Converted)` stay excluded unless listed
    in `stages`.
    """
    field_list = ", ".join(fields)
    classified_sql = "true" if llm_classified else "false"
    wanted_type = (site_type or "").strip()
    clauses: list[str] = []
    if wanted_type.lower() not in {"any", "all", "*", "none"}:
        if wanted_type:
            clauses.append(f"Site_Type__c = {_soql_quote(wanted_type)}")
        else:
            clauses.append("(Site_Type__c = null OR Site_Type__c = '')")
    clauses.extend(
        [
            f"LLM_Classified__c = {classified_sql}",
            "(LLM_Holdout__c = false OR LLM_Holdout__c = null)",
            "Site_Latitude__c != null AND Site_Latitude__c != ''",
            "Site_Longitude__c != null AND Site_Longitude__c != ''",
            f"Stage__c IN ({_soql_in(stages)})",
        ]
    )
    owner_list = [str(v).strip() for v in (owners or []) if str(v).strip()]
    if owner_list:
        clauses.append(f"Owner__c IN ({_soql_in(owner_list)})")
    excluded_owners = [
        str(v).strip() for v in (exclude_owners or []) if str(v).strip()
    ]
    if excluded_owners:
        clauses.append(
            "(Owner__c = null OR Owner__c = '' OR "
            f"Owner__c NOT IN ({_soql_in(excluded_owners)}))"
        )
    requested = {str(s).strip() for s in stages if str(s).strip()}
    excluded = [
        stage for stage in EXCLUDED_STAGE_FILTER if stage not in requested
    ]
    if excluded:
        clauses.append(f"Stage__c NOT IN ({_soql_in(excluded)})")
    carrier = (carrier_like or "").strip()
    if carrier:
        escaped = carrier.replace("\\", "\\\\").replace("'", "\\'")
        clauses.insert(1, f"Carrier_Leasing_Source__c LIKE '%{escaped}%'")
    metros = [m.strip() for m in (metro_classification or "").split(",") if m.strip()]
    if len(metros) == 1:
        clauses.insert(1, f"Metro_Classification__c = {_soql_quote(metros[0])}")
    elif metros:
        clauses.insert(1, f"Metro_Classification__c IN ({_soql_in(metros)})")
    clean_states = [
        str(s).strip().upper() for s in (states or []) if str(s).strip()
    ]
    if clean_states:
        clauses.append(f"Site_State__c IN ({_soql_in(clean_states)})")
    return (
        f"SELECT {field_list} FROM {OBJECT_NAME} WHERE "
        + " AND ".join(clauses)
        + " ORDER BY Id"
    )


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
    owners: Sequence[str] | None = DEFAULT_OWNER_FILTER,
    exclude_owners: Sequence[str] | None = None,
    carrier_like: str | None = None,
    metro_classification: str | None = "Major NFL Metro",
    states: Sequence[str] | None = None,
    llm_classified: bool = False,
    site_type: str | None = None,
) -> list[dict[str, Any]]:
    soql = build_blank_site_type_query(
        stages=stages,
        owners=owners,
        exclude_owners=exclude_owners,
        carrier_like=carrier_like,
        metro_classification=metro_classification,
        states=states,
        llm_classified=llm_classified,
        site_type=site_type,
    )
    logger.info("Salesforce SOQL: %s", soql)
    return query_all(client, soql)


# Stay under Salesforce SOQL length limits on large chip-reuse reruns.
_SF_ID_QUERY_CHUNK = 200


def query_sites_by_ids(
    client: SalesforceClient,
    ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Fetch Site__c rows by Id, preserving the requested order."""
    clean = [str(sid).strip() for sid in ids if str(sid).strip()]
    by_id: dict[str, dict[str, Any]] = {}
    for start in range(0, len(clean), _SF_ID_QUERY_CHUNK):
        chunk = clean[start : start + _SF_ID_QUERY_CHUNK]
        soql = build_sites_by_ids_query(chunk)
        logger.info("Salesforce SOQL: %s", soql)
        for row in query_all(client, soql):
            key = str(row.get("Id") or "").strip()
            if key:
                by_id[key] = row
    ordered: list[dict[str, Any]] = []
    for sid in clean:
        if sid in by_id:
            ordered.append(by_id[sid])
    return ordered


def parse_sf_lat_lng(row: dict[str, Any]) -> tuple[float, float] | None:
    lat_raw = row.get("Site_Latitude__c")
    lng_raw = row.get("Site_Longitude__c")
    if is_missing(lat_raw) or is_missing(lng_raw):
        return None
    try:
        return float(lat_raw), float(lng_raw)
    except (TypeError, ValueError):
        return None


# When the Salesforce write fails, dequeue with the same flags as imagery holdouts.
# LLM_Classified=false + LLM_Holdout=true leaves the blank-Site_Type queue.
APPLY_ERROR_HOLDOUT_PAYLOAD = {
    "LLM_Classified__c": False,
    "LLM_Holdout__c": True,
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


def apply_queue_flags(
    payload: dict[str, Any],
    *,
    write_holdout: bool = True,
) -> dict[str, Any]:
    """Set LLM_Classified / LLM_Holdout from whether site enrichment fields are present.

    Successful enrichment: LLM_Classified=true, LLM_Holdout=false.
    Holdout / dequeue-only: LLM_Classified=false, LLM_Holdout=true.
    ``write_holdout=False`` (DB-only batch): LLM_Classified=true for every
    processed row (hits and misses). Hits also get site fields. Do not write
    LLM_Holdout.
    """
    out = dict(payload)
    is_enrich = is_enrichment_payload(out)
    if write_holdout:
        out["LLM_Classified__c"] = is_enrich
        out["LLM_Holdout__c"] = not is_enrich
    else:
        out["LLM_Classified__c"] = True
    return out


def _is_holdout_fallback_payload(payload: dict[str, Any] | None) -> bool:
    """True when this payload is the error/holdout dequeue (no site fields)."""
    if not payload:
        return False
    return (
        payload.get("LLM_Classified__c") is False
        and payload.get("LLM_Holdout__c") is True
        and "Site_Type__c" not in payload
        and "Site_Latitude__c" not in payload
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


def build_row_payload(row: dict[str, Any], *, write_holdout: bool = True) -> dict[str, Any]:
    """Salesforce payload for one detail row (site fields + queue flags)."""
    from enrichment.connectx_audit import (
        audit_holdout_payload,
        confirmed_owner_id,
        is_audit_holdout_row,
        is_unqualify_row,
        unqualify_payload,
    )

    if is_audit_holdout_row(row):
        return audit_holdout_payload(row)
    if is_unqualify_row(row):
        return unqualify_payload(row)
    payload = row.get("payload")
    if not isinstance(payload, dict):
        site_fields = build_update_payload(
            latitude=to_float(row.get("update_lat")),
            longitude=to_float(row.get("update_lng")),
            site_type=(row.get("update_site_type") or None) or None,
            verified_site=to_bool(row.get("update_verified_site")),
            verified_site_source=(row.get("update_verified_site_source") or None)
            or None,
        )
        payload = apply_queue_flags(site_fields, write_holdout=write_holdout)
        owner_id = confirmed_owner_id(row)
        if owner_id:
            payload["OwnerId"] = owner_id
        return payload
    # Preserve explicit queue flags on prebuilt payloads; fill any gaps.
    payload = dict(payload)
    is_enrich = is_enrichment_payload(payload)
    if "LLM_Classified__c" not in payload:
        payload["LLM_Classified__c"] = True if not write_holdout else is_enrich
    if write_holdout and "LLM_Holdout__c" not in payload:
        payload["LLM_Holdout__c"] = not is_enrich
    if not write_holdout:
        payload.pop("LLM_Holdout__c", None)
    return payload


def _row_sf_id(row: dict[str, Any]) -> str:
    return str(row.get("Id") or row.get("sf_id") or "").strip()


def validate_row_payload(row: dict[str, Any], payload: dict[str, Any]) -> None:
    """Raise ValueError when a row must not write these site fields."""
    if not _row_sf_id(row):
        raise ValueError("Missing Salesforce Id")
    naip_site_type = str(row.get("naip_site_type") or "").strip().lower()
    payload_site_type = str(payload.get("Site_Type__c") or "").strip()
    db_skip = str(row.get("holdout_reason") or "").strip() == "skip_classify_db_hit"
    if is_enrichment_payload(payload) and naip_site_type not in {"tower", "rooftop"}:
        if not (db_skip and payload_site_type):
            raise ValueError(
                f"Salesforce updates require NAIP site_type=tower|rooftop; got "
                f"{naip_site_type or 'blank'}"
            )
    if not payload:
        raise ValueError("Empty update payload")


def _new_entry(sf_id: str, payload: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    return {
        "Id": sf_id,
        "success": False,
        "dry_run": dry_run,
        "error": "",
        "status": "",
        "payload": payload,
    }


def _needs_error_fallback(
    entry: dict[str, Any], *, error_holdout: bool, dry_run: bool
) -> bool:
    return (
        error_holdout
        and not dry_run
        and bool(entry["Id"])
        and not _is_holdout_fallback_payload(entry["payload"])
    )


def _mark_fallback(entry: dict[str, Any], error: str, fallback_error: str | None) -> None:
    """Record the outcome of the LLM_Holdout retry after a failed write."""
    original = entry["error"]
    entry["payload"] = dict(APPLY_ERROR_HOLDOUT_PAYLOAD)
    if fallback_error is None:
        entry["success"] = True
        entry["status"] = "updated_holdout_after_error"
        entry["error"] = f"fallback after apply error: {original}"
        logger.warning(
            "SF write failed for %s — dequeued LLM_Holdout=true: %s", entry["Id"], error
        )
    else:
        entry["status"] = "failed"
        entry["error"] = (
            f"holdout fallback also failed: {fallback_error} (original: {original})"
        )
        logger.warning("holdout fallback failed for %s: %s", entry["Id"], fallback_error)


def apply_one_update(
    client: SalesforceClient,
    row: dict[str, Any],
    *,
    dry_run: bool = False,
    verbose: bool = True,
    write_holdout: bool = True,
    error_holdout: bool = True,
) -> dict[str, Any]:
    """Update a single enrichment candidate row; never raises.

    Eligible tower/rooftop writes: LLM_Classified__c=true, LLM_Holdout__c=false.
    Holdouts (no site fields): LLM_Classified__c=false, LLM_Holdout__c=true so
    they leave the blank-Site_Type enrichment queue and are flagged as holdouts.
    ``write_holdout=False`` (DB-only): successful rows get LLM_Classified=true
    and no LLM_Holdout. Hits also get site type/coords.

    If the Salesforce write fails (duplicates, API errors), retries once with
    LLM_Classified=false + LLM_Holdout=true so the site still dequeues, unless
    ``error_holdout=False`` (rooftop NAIP confirm: leave the row unchanged).
    """
    from enrichment import progress

    sf_id = _row_sf_id(row)
    payload = build_row_payload(row, write_holdout=write_holdout)
    entry = _new_entry(sf_id, payload, dry_run=dry_run)
    try:
        validate_row_payload(row, payload)
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
        return entry
    except Exception as exc:  # noqa: BLE001 — per-row resilience
        entry["error"] = str(exc)
        entry["status"] = "failed"
        if not _needs_error_fallback(entry, error_holdout=error_holdout, dry_run=dry_run):
            logger.warning("update failed for %s: %s", sf_id, exc)
            if verbose:
                progress.warn(f"SF update failed — continuing: {exc}")
            return entry
    try:
        update_site(client, sf_id, dict(APPLY_ERROR_HOLDOUT_PAYLOAD), verbose=False)
        _mark_fallback(entry, entry["error"], None)
        if verbose:
            progress.warn(
                "SF write failed — dequeued (LLM_Classified=false, LLM_Holdout=true)"
            )
    except Exception as fallback_exc:  # noqa: BLE001
        _mark_fallback(entry, entry["error"], str(fallback_exc))
        if verbose:
            progress.warn(f"SF update failed — continuing: {entry['error']}")
    return entry


def status_from_entry(entry: dict[str, Any]) -> str:
    """detail-row ``sf_update_status`` for one apply result."""
    if entry.get("dry_run"):
        return "dry_run"
    if not entry.get("success"):
        return "failed"
    payload = entry.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("Stage__c") == "Unqualified":
        return "unqualified"
    if is_enrichment_payload(payload):
        return "updated"
    if payload.get("LLM_Holdout__c") is True:
        return "dequeued"
    if payload.get("LLM_Classified__c"):
        return "classified_only"
    return "dequeued"


def _format_apply_result(
    payload: dict[str, Any],
    *,
    dry_run: bool,
    status: str = "",
) -> str:
    """Human-readable apply line: enrichment details, or holdout dequeue."""
    prefix = "SF dry-run OK (not written)" if dry_run else "SF updated"
    if status == "updated_holdout_after_error":
        return (
            f"{prefix} | apply error fallback | "
            "dequeued (LLM_Classified=false, LLM_Holdout=true)"
        )
    if payload.get("Stage__c") == "Unqualified":
        return (
            f"{prefix} | Unqualified ({payload.get('Unqualified_Reason__c') or '—'}) | "
            f"owner={payload.get('OwnerId') or 'unchanged'}"
        )
    if not is_enrichment_payload(payload):
        if payload.get("LLM_Classified__c") and payload.get("LLM_Holdout__c") is not True:
            return f"{prefix} | LLM_Classified=true (no site type)"
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


# ---------------------------- sObject Collections ---------------------------

# Salesforce caps sObject Collections at 200 records per request.
COLLECTION_LIMIT = 200


def _collection_errors(result: dict[str, Any]) -> str:
    errors = result.get("errors") or []
    return "; ".join(
        f"{err.get('statusCode') or 'ERROR'}: {err.get('message') or ''}".strip()
        for err in errors
        if isinstance(err, dict)
    ) or "unknown Salesforce error"


def update_sites_collection(
    client: SalesforceClient,
    updates: Sequence[tuple[str, dict[str, Any]]],
) -> list[str | None]:
    """PATCH up to 200 Site__c rows in one call. Per-row error text or None.

    ``allOrNone=false``: each record succeeds or fails independently.
    Raises only when the whole request fails (transport / auth).
    """
    if not updates:
        return []
    if len(updates) > COLLECTION_LIMIT:
        raise ValueError(f"at most {COLLECTION_LIMIT} records per collection call")
    body = {
        "allOrNone": False,
        "records": [
            {"attributes": {"type": OBJECT_NAME}, "id": sf_id, **payload}
            for sf_id, payload in updates
        ],
    }
    response = client.sf.restful("composite/sobjects", method="PATCH", json=body)
    if not isinstance(response, list) or len(response) != len(updates):
        raise RuntimeError(f"unexpected sObject Collections reply: {response!r}")
    return [
        None if (item or {}).get("success") else _collection_errors(item or {})
        for item in response
    ]


def _send_updates(
    client: SalesforceClient, updates: Sequence[tuple[str, dict[str, Any]]]
) -> list[str | None]:
    """One PATCH for a single row, sObject Collections for several."""
    if len(updates) == 1:
        sf_id, payload = updates[0]
        try:
            update_site(client, sf_id, payload, verbose=False)
            return [None]
        except Exception as exc:  # noqa: BLE001
            return [str(exc)]
    return update_sites_collection(client, updates)


def apply_updates_batch(
    client: SalesforceClient,
    rows: Iterable[dict[str, Any]],
    *,
    dry_run: bool = False,
    verbose: bool = True,
    write_holdout: bool = True,
    error_holdout: bool = True,
    chunk_size: int = COLLECTION_LIMIT,
) -> list[dict[str, Any]]:
    """Apply many rows with the same rules as ``apply_one_update``.

    Rows go out in sObject Collections chunks. Failed records get the same
    LLM_Holdout retry, also batched. If a whole collection call fails, that
    chunk falls back to row-by-row ``apply_one_update``.
    """
    from enrichment import progress

    rows_list = list(rows)
    chunk_size = max(1, min(COLLECTION_LIMIT, int(chunk_size)))
    entries: list[dict[str, Any]] = []
    for start in range(0, len(rows_list), chunk_size):
        chunk_entries: list[dict[str, Any]] = []
        prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in rows_list[start : start + chunk_size]:
            payload = build_row_payload(row, write_holdout=write_holdout)
            entry = _new_entry(_row_sf_id(row), payload, dry_run=dry_run)
            try:
                validate_row_payload(row, payload)
            except ValueError as exc:
                entry["error"] = str(exc)
                entry["status"] = "failed"
            else:
                if dry_run:
                    entry["success"] = True
                    entry["status"] = "dry_run"
                else:
                    prepared.append((row, entry))
            chunk_entries.append(entry)
        entries.extend(chunk_entries)
        if dry_run:
            continue
        handled: set[int] = set()
        if prepared:
            try:
                errors = _send_updates(
                    client, [(entry["Id"], entry["payload"]) for _row, entry in prepared]
                )
            except Exception as exc:  # noqa: BLE001 — whole call failed
                logger.warning(
                    "sObject Collections call failed (%s); applying row by row", exc
                )
                for row, entry in prepared:
                    entry.update(
                        apply_one_update(
                            client,
                            row,
                            dry_run=False,
                            verbose=False,
                            write_holdout=write_holdout,
                            error_holdout=error_holdout,
                        )
                    )
                    handled.add(id(entry))
            else:
                for (_row, entry), error in zip(prepared, errors):
                    if error is None:
                        entry["success"] = True
                        entry["status"] = "updated"
                    else:
                        entry["error"] = error
                        entry["status"] = "failed"
        _fallback_failed(
            client,
            [entry for entry in chunk_entries if id(entry) not in handled],
            error_holdout=error_holdout,
        )
    for index, entry in enumerate(entries, start=1):
        entry["index"] = index
        if verbose:
            if entry.get("success"):
                progress.result(
                    f"{entry['Id']} | "
                    + _format_apply_result(
                        entry.get("payload") or {},
                        dry_run=dry_run,
                        status=str(entry.get("status") or ""),
                    )
                )
            else:
                progress.warn(
                    f"{entry['Id']} | SF update failed — continuing: "
                    f"{entry.get('error') or 'unknown'}"
                )
    return entries


def _fallback_failed(
    client: SalesforceClient,
    entries: list[dict[str, Any]],
    *,
    error_holdout: bool,
) -> None:
    """LLM_Holdout retry for every failed entry that qualifies (one batch)."""
    retry = [
        entry
        for entry in entries
        if entry["status"] == "failed"
        and _needs_error_fallback(entry, error_holdout=error_holdout, dry_run=False)
    ]
    if not retry:
        return
    payload = dict(APPLY_ERROR_HOLDOUT_PAYLOAD)
    try:
        errors = _send_updates(client, [(entry["Id"], payload) for entry in retry])
    except Exception as exc:  # noqa: BLE001
        errors = [str(exc)] * len(retry)
    for entry, error in zip(retry, errors):
        _mark_fallback(entry, entry["error"], error)
