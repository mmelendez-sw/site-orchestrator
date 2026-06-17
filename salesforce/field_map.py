"""Canonical record key -> Salesforce API field names."""

FIELD_MAP: dict[str, str] = {
    "lat": "Latitude__c",
    "lng": "Longitude__c",
    "address": "Address__c",
    "permit_metadata": "Permit_Metadata__c",
    "site_type": "Site_Type__c",
    "site_confidence": "Site_Confidence__c",
    "cell_equipment": "Cell_Equipment__c",
    "source_url": "Source_URL__c",
}

OBJECT_NAME = "Site__c"
DUPLICATE_LOG_OBJECT = "Site_Duplicate_Log__c"
