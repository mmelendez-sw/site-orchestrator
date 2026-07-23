"""Canonical record key -> Salesforce API field names."""

import os

# Only fields that exist on Site__c and belong on the upload template.
# Internal keys (permit_metadata, site_confidence, cell_equipment, source_url)
# stay on the local upload dict for mapping/debug — they are not sent to SF.
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
    "owner_id": "OwnerId",
    "site_type": "Site_Type__c",
    "verified_site": "Verified_Site__c",
    "verified_site_source": "Verified_Site_Source__c",
    "morphology": "Morphology__c",
    # "property_type": "Property_Type__c",
}

OBJECT_NAME = "Site__c"


def _apply_env_overrides() -> None:
    """Allow org-specific API names via SF_FIELD_<KEY> env vars."""
    for key in list(FIELD_MAP):
        env_key = f"SF_FIELD_{key.upper()}"
        override = os.environ.get(env_key, "").strip()
        if override:
            FIELD_MAP[key] = override


_apply_env_overrides()
