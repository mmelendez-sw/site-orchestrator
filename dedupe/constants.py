"""Dedupe thresholds and Salesforce field defaults."""

DUPLICATE_THRESHOLD = 85
REVIEW_THRESHOLD = 60
DEFAULT_RADIUS_METERS = 250

# Urbanicity tiers from ZCTA population (see data/zip_populations.csv).
URBAN_POPULATION_MIN = 25_000
SUBURBAN_POPULATION_MIN = 2_500
URBAN_RADIUS_M = 50
SUBURBAN_RADIUS_M = 150
RURAL_RADIUS_M = 250
URBANICITY_DEFAULT_TIER = "suburban"

# Combined dedupe score weights (address fuzzy match + in-radius proximity).
ADDRESS_SCORE_WEIGHT = 0.65
PROXIMITY_SCORE_WEIGHT = 0.35

# Max distance (meters) for an outside-radius address match to count as review/duplicate.
# Beyond this, fuzzy-only matches from the zip/bbox pool are treated as net_new.
OUTSIDE_RADIUS_REVIEW_MAX_M = 500

SF_OBJECT_NAME = "Site__c"
SF_LAT_FIELD = "Site_Latitude__c"
SF_LNG_FIELD = "Site_Longitude__c"
SF_ZIP_FIELD = "Site_Zip_Code__c"
SF_ADDRESS_FIELD = "Site_Address__c"
SF_CITY_FIELD = "Site_City__c"
SF_STATE_FIELD = "Site_State__c"
