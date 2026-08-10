"""Constants for FCC / TowerSource / NAIP enrichment."""

from __future__ import annotations

PROXIMITY_MAX_M = 25.0

# Imagery asset-box snap radius from the SF/classify pin. Effective max is
# MAX_ASSET_OFFSET_M + ASSET_OFFSET_LEEWAY_M. Beyond that: rooftops hold out;
# imagery-only towers hold out (DB hits may still update). Independent of
# PROXIMITY_MAX_M.
MAX_ASSET_OFFSET_M = 75.0
ASSET_OFFSET_LEEWAY_M = 10.0

# Degrees buffer used for SQL bbox prefilter (~25–30 m at mid-latitudes).
BBOX_BUFFER_DEG = 0.0003

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
    "LLM_Classified__c",
    "LLM_Holdout__c",
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
    "Marketing Campaign",
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
REVIEW_DIR_NAME = "review"
REVIEW_MANIFEST_CSV = "review_manifest.csv"
REVIEW_INDEX_HTML = "index.html"

# Medium/high confidence required for automatic update candidacy (DB hits).
MIN_UPDATE_CONFIDENCE = 0.6

# Rooftop cell gear must clear this bar (explicit confidence required).
MIN_ROOFTOP_CELL_CONFIDENCE = 0.75

# Stricter bars when there is no FCC/TowerSource proximity hit.
# Slightly below prior 0.85 so strong Nearmap rooftops are not lost on conf alone.
MIN_IMAGERY_ONLY_SITE_CONFIDENCE = 0.80
MIN_IMAGERY_ONLY_CELL_CONFIDENCE = 0.80

# When Gemini cell conf is in this band, Claude confirmation is mandatory
# before a rooftop SF write (also required whenever Gemini cell=true).
MIN_ENSEMBLE_CELL_CONFIDENCE = 0.85

CELL_GEAR_KINDS = (
    "sector_panel",
    "facade_mount",
    "microwave",
    "rru",
    "parapet_mast",
    "none",
    "unclear",
)
