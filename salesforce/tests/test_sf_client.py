"""Placeholder tests for Salesforce client."""

from salesforce.field_map import FIELD_MAP
from salesforce.sf_client import SalesforceClient


def test_field_map_includes_core_site_fields():
    assert FIELD_MAP["lat"] == "Latitude__c"
    assert FIELD_MAP["address"] == "Address__c"


def test_create_site_maps_payload():
    # TODO: mock Salesforce create and assert mapped fields
    client = SalesforceClient
    assert client is not None


def test_log_duplicate_maps_audit_fields():
    # TODO: mock Salesforce create on duplicate log object
    assert "permit_metadata" in FIELD_MAP
