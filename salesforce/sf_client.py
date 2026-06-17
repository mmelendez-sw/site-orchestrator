"""Salesforce client for site creation and duplicate audit logging."""

from __future__ import annotations

import json
import os
from typing import Any

from simple_salesforce import Salesforce

from salesforce.field_map import DUPLICATE_LOG_OBJECT, FIELD_MAP, OBJECT_NAME


class SalesforceClient:
    """Authenticate and load site records into Salesforce."""

    def __init__(self) -> None:
        self.sf = Salesforce(
            username=os.environ["SF_USERNAME"],
            password=os.environ["SF_PASSWORD"],
            security_token=os.environ["SF_SECURITY_TOKEN"],
            domain=os.environ.get("SF_DOMAIN", "login"),
        )

    def record_exists(self, record_id: str) -> bool:
        """Return True if a Salesforce record with the given Id exists."""
        try:
            self.sf.query(f"SELECT Id FROM {OBJECT_NAME} WHERE Id = '{record_id}' LIMIT 1")
            return True
        except Exception:
            return False

    def _map_record(self, record: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, sf_field in FIELD_MAP.items():
            if key not in record or record[key] is None:
                continue
            value = record[key]
            if key == "permit_metadata" and isinstance(value, dict):
                value = json.dumps(value)
            payload[sf_field] = value
        return payload

    def create_site(self, record: dict[str, Any]) -> dict[str, Any]:
        """Create a new Site record from a canonical + classification dict."""
        payload = self._map_record(record)
        result = getattr(self.sf, OBJECT_NAME).create(payload)
        return dict(result)

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
