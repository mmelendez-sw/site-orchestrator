"""Placeholder tests for Salesforce client."""

from salesforce.field_map import FIELD_MAP
from salesforce.sf_client import SalesforceClient


def test_field_map_includes_core_site_fields():
    assert FIELD_MAP["lat"] == "Site_Latitude__c"
    assert FIELD_MAP["lng"] == "Site_Longitude__c"
    assert FIELD_MAP["address"] == "Site_Address__c"
    assert FIELD_MAP["owner_id"] == "OwnerId"
    assert FIELD_MAP["site_state"] == "Site_State__c"


def test_create_site_maps_payload():
    # TODO: mock Salesforce create and assert mapped fields
    client = SalesforceClient
    assert client is not None


def test_field_map_includes_permit_metadata():
    assert "permit_metadata" in FIELD_MAP
