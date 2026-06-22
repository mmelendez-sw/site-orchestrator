"""Canonical record key -> Salesforce API field names."""

import os

FIELD_MAP: dict[str, str] = {
    "lat": "Site_Latitude__c",
    "lng": "Site_Longitude__c",
    "zip_code": "Site_Zip_Code__c",
    "address": "Site_Address__c",
    "site_street": "Site_Street__c",
    "site_city": "Site_City__c",
    "site_state": "Site_State__c",
    "site_country": "Site_Country__c",
    "carrier_leasing_source": "Carrier_Leasing_Source__c",
    "permit_metadata": "Permit_Metadata__c",
    "site_type": "Site_Type__c",
    "site_confidence": "Site_Confidence__c",
    "cell_equipment": "Cell_Equipment__c",
    "source_url": "Source_URL__c",
    "verified_site": "Verified_Site__c",
    "verified_site_source": "Verified_Site_Source__c",
    "morphology": "Morphology__c",
    # "property_type": "Property_Type__c",
}

OBJECT_NAME = "Site__c"
DUPLICATE_LOG_OBJECT = "Site_Duplicate_Log__c"


def _apply_env_overrides() -> None:
    """Allow org-specific API names via SF_FIELD_<KEY> env vars."""
    for key in list(FIELD_MAP):
        env_key = f"SF_FIELD_{key.upper()}"
        override = os.environ.get(env_key, "").strip()
        if override:
            FIELD_MAP[key] = override


_apply_env_overrides()
