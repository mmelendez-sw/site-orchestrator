"""Constants for FCC / TowerSource / NAIP enrichment."""

from __future__ import annotations

PROXIMITY_MAX_M = 50.0

# Degrees buffer used for SQL bbox prefilter (~55 m at mid-latitudes).
BBOX_BUFFER_DEG = 0.0006

FCC_TABLE = "dbo.FCCTowerData"
TOWERSOURCE_TABLE = "dbo.TowerSourceASRTowers"

SF_QUERY_FIELDS = (
    "Id",
    "Site_Latitude__c",
    "Site_Longitude__c",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "Site_Zip_Code__c",
    "Site_Type__c",
    "Carrier_Leasing_Source__c",
    "Stage__c",
    "Owner__c",
    "Verified_Site__c",
    "Verified_Site_Source__c",
)

DEFAULT_STAGE_FILTER = (
    "Enhanced/Unreviewed",
    "New/Unreviewed",
    "Outreach",
)

# Hard exclusion applied independently of the caller-provided stage filter.
EXCLUDED_STAGE_FILTER = (
    "Working-Connected",
    "Qualified (Converted)",
)

DEFAULT_OWNER_FILTER = (
    "Site Acquisition Team",
    "Matthew Melendez",
)

MATCH_SOURCE_FCC = "FCC"
MATCH_SOURCE_TOWERSOURCE = "TowerSource"
MATCH_SOURCE_NONE = "none"

VERIFIED_SITE_SOURCE_FCC = "FCC"
VERIFIED_SITE_SOURCE_TOWERSOURCE = "TowerSource"
VERIFIED_SITE_SOURCE_NAIP = "NAIP"
VERIFIED_SITE_SOURCE_NEARMAP = "NearMap"

BUCKET_POTENTIAL_UPDATE = "potential_update"
BUCKET_ROOFTOP = "potential_rooftop"
BUCKET_OTHER = "other_or_else"
BUCKET_SKIP = "skip"

CANDIDATE_CSV = "potential_sf_updates.csv"
HOLDOUT_CSV = "holdout_rooftop_other.csv"
DETAIL_CSV = "enrichment_detail.csv"
APPLY_LOG_CSV = "sf_update_apply_log.csv"

# Medium/high confidence required for automatic update candidacy.
MIN_UPDATE_CONFIDENCE = 0.6
