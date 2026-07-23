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


def test_field_map_excludes_internal_only_keys():
    # Local pipeline keys — not Salesforce Site__c upload fields.
    assert "permit_metadata" not in FIELD_MAP
    assert "site_confidence" not in FIELD_MAP
    assert "cell_equipment" not in FIELD_MAP
    assert "source_url" not in FIELD_MAP


def test_map_upload_skips_nan_and_internal_keys():
    from salesforce.sf_client import map_upload_record_to_payload

    payload = map_upload_record_to_payload(
        {
            "lat": 43.0,
            "lng": -88.0,
            "site_street": "123 Main St",
            "site_city": "Milwaukee",
            "site_state": "WI",
            "zip_code": "53202",
            "site_country": "US",
            "carrier_leasing_source": "JF_PermitScraping_jul26",
            "owner_id": "0056O00000EpUOgQAN",
            "site_type": "Rooftop",
            "verified_site": "TRUE",
            "verified_site_source": "Permitting Data",
            "morphology": "Urban",
            "permit_metadata": {"scope_state": "WI"},
            "site_confidence": float("nan"),
            "cell_equipment": float("nan"),
        }
    )
    assert "Permit_Metadata__c" not in payload
    assert "Site_Confidence__c" not in payload
    assert "Cell_Equipment__c" not in payload
    assert payload["Site_State__c"] == "WI"
    assert payload["Site_Type__c"] == "Rooftop"
