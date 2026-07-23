"""Salesforce client for site creation and duplicate audit logging."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from simple_salesforce import Salesforce

from salesforce.field_map import DUPLICATE_LOG_OBJECT, FIELD_MAP, OBJECT_NAME
from salesforce.upload_template import validate_upload_record

logger = logging.getLogger(__name__)


class SalesforceClient:
    """Authenticate and load site records into Salesforce."""

    def __init__(self) -> None:
        self.sf = Salesforce(
            username=os.environ["SF_USERNAME"],
            password=os.environ["SF_PASSWORD"],
            security_token=os.environ["SF_SECURITY_TOKEN"],
            domain=os.environ.get("SF_DOMAIN", "login"),
        )
        instance = getattr(self.sf, "sf_instance", None) or getattr(self.sf, "base_url", "")
        logger.info("Salesforce authenticated — API instance: %s", instance)

    def record_exists(self, record_id: str) -> bool:
        """Return True if a Salesforce record with the given Id exists."""
        try:
            self.sf.query(f"SELECT Id FROM {OBJECT_NAME} WHERE Id = '{record_id}' LIMIT 1")
            return True
        except Exception:
            return False

    def _map_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return map_upload_record_to_payload(record)

    def create_site(self, record: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
        """Create a new Site record from a canonical + classification dict."""
        errors = validate_upload_record(record)
        if errors:
            raise ValueError(
                "Upload record failed validation: " + "; ".join(errors[:5])
            )
        payload = self._map_record(record)
        if verbose:
            logger.info("  SF payload: %s", json.dumps(payload, default=str))
        result = getattr(self.sf, OBJECT_NAME).create(payload)
        created = dict(result)
        if verbose:
            logger.info("  Salesforce create OK — Id=%s success=%s", created.get("id"), created.get("success"))
        return created

    def create_sites(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create multiple Site records; raises on first validation/API failure."""
        results: list[dict[str, Any]] = []
        for record in records:
            results.append(self.create_site(record))
        return results

    def log_duplicate(self, record: dict[str, Any], matched_id: str) -> dict[str, Any]:
        """Log a duplicate match for audit purposes."""
        payload = {
            "Matched_Site__c": matched_id,
            "Incoming_Address__c": record.get("address"),
            "Incoming_Latitude__c": record.get("lat"),
            "Incoming_Longitude__c": record.get("lng"),
            "Permit_Metadata__c": json.dumps(record.get("permit_metadata") or {}),
        }
        result = getattr(self.sf, DUPLICATE_LOG_OBJECT).create(payload)
        return dict(result)


def map_upload_record_to_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Map a build_upload_record dict to Salesforce API field names (no API call)."""
    payload: dict[str, Any] = {}
    for key, sf_field in FIELD_MAP.items():
        if key == "address":
            # Site_Address__c is used for dedupe reads; UAT treats it as read-only on insert.
            # Street/city/state/zip fields populate the record instead.
            continue
        if key not in record or record[key] is None:
            continue
        value = record[key]
        if isinstance(value, str) and not value.strip():
            # Omit blank optional fields (e.g. rooftop without cell_equipment -> no Site Type).
            continue
        if key == "permit_metadata" and isinstance(value, dict):
            value = json.dumps(value)
        elif key == "verified_site":
            value = _coerce_bool(value)
        payload[sf_field] = value
    return payload


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().upper()
    if text in {"TRUE", "1", "YES"}:
        return True
    if text in {"FALSE", "0", "NO"}:
        return False
    raise ValueError(f"Invalid boolean value for Verified Site: {value!r}")


def _compose_full_address(record: dict[str, Any]) -> str:
    parts = [
        record.get("site_street"),
        record.get("site_city"),
        record.get("site_state"),
        record.get("zip_code"),
    ]
    return ", ".join(str(part) for part in parts if part)
