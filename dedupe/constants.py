"""Dedupe thresholds and Salesforce field defaults."""

DUPLICATE_THRESHOLD = 85
REVIEW_THRESHOLD = 60
AUTO_REJECT_ADDRESS_SCORE = 50
DEFAULT_RADIUS_METERS = 250
THRESHOLD_VERSION = "2026-06-18-dc"

# Urbanicity tiers from ZCTA population (nationwide CSV — see ZIP_POPULATION_CSV).
URBAN_POPULATION_MIN = 25_000
SUBURBAN_POPULATION_MIN = 2_500
URBAN_RADIUS_M = 100
SUBURBAN_RADIUS_M = 100
RURAL_RADIUS_M = 250
URBANICITY_DEFAULT_TIER = "suburban"

# DC dense urban search radius (40–80m band; demographics-us.com zips).
DC_ZIP_PREFIXES = ("200", "202", "203", "204", "205")
DC_STATE_TOKENS = ("DC", "DISTRICT OF COLUMBIA")
DC_DENSE_RADIUS_M = 60

# Combined dedupe score weights (address fuzzy match + in-radius proximity).
ADDRESS_SCORE_WEIGHT = 0.65
PROXIMITY_SCORE_WEIGHT = 0.35

# Weighted address_score components (R06).
ADDRESS_COMPONENT_HOUSE_WEIGHT = 0.40
ADDRESS_COMPONENT_STREET_WEIGHT = 0.40
ADDRESS_COMPONENT_SUFFIX_WEIGHT = 0.20

# Tiered high-address override (cap at 2x urbanicity prefilter radius — R4).
HIGH_ADDRESS_EXACT_MIN = 98
HIGH_ADDRESS_STRONG_MIN = 90
HIGH_ADDRESS_RADIUS_MULTIPLIER = 2.0

# Hard gates (R03–R05).
STREET_NAME_JACCARD_MIN = 0.50
HOUSE_NUMBER_DELTA_REJECT = 20
HOUSE_NUMBER_DELTA_PENALTY_START = 10
HOUSE_NUMBER_DELTA_AUTO_NET_NEW_MAX = 10
CITY_MISMATCH_REJECT_MIN_M = 15

# Geocoder collision — very close pins but clearly different addresses.
GEOCODER_COLLISION_MAX_M = 25
GEOCODER_COLLISION_MAX_ADDRESS = 60
GEOCODER_COLLISION_JACCARD_MIN = 0.50

# Address-score floor (house-number mismatch cap) + moderate proximity → review (R6).
ADDRESS_FLOOR_SCORE = 45
ADDRESS_FLOOR_PROXIMITY_MIN = 50

# Zip mismatch at low distance — surface for review unless high-address duplicate (R8).
ZIP_MISMATCH_REVIEW_MAX_M = 50

# City mismatch with high confidence still needs review (R14).
CITY_MISMATCH_REVIEW_MIN_COMBINED = 60

# Downweight proximity when address agreement is weak (R07).
PROXIMITY_DOWNWEIGHT_ADDRESS_MAX = 50

# Runner-up tie detection (R10).
TIE_BREAKER_CLOSE_MAX_DELTA = 15

# House-number delta + distance → net-new (DC rule 6).
HOUSE_NUMBER_FAR_DELTA_MIN = 10
HOUSE_NUMBER_FAR_DISTANCE_M = 50

# Borderline potential-duplicate band (DC rule 9).
POTENTIAL_DUPLICATE_BORDERLINE_MIN = 30
POTENTIAL_DUPLICATE_BORDERLINE_MAX = 65

# Outside-radius fuzzy matches never promote to review/duplicate (in-radius only).
OUTSIDE_RADIUS_REVIEW_MAX_M = 0

# Only fuzzy-score Salesforce candidates within this distance of the incoming pin.
FUZZY_PREFILTER_MAX_M = 500

# Flag net-new rows that are close and moderately similar for manual calibration.
POTENTIAL_DUPLICATE_MIN_COMBINED = 50
POTENTIAL_DUPLICATE_MAX_DISTANCE_M = 100

# Proximity-aware promotion when address match is weak but coordinates agree.
PROX_DUPLICATE_MAX_M = 25
PROX_DUPLICATE_MIN_ADDRESS = 75
PROX_REVIEW_MAX_M = 50
PROX_REVIEW_MIN_ADDRESS = 70
PROX_REVIEW_EXTENDED_MAX_M = 100
PROX_REVIEW_EXTENDED_MIN_ADDRESS = 80

# Input batch self-dedupe coordinate rounding (degrees).
INPUT_DEDUPE_COORD_PRECISION = 5

# Coordinate-near input dedupe (same building, slightly different geocode/string).
INPUT_NEAR_DEDUPE_MAX_M = 15
INPUT_NEAR_DEDUPE_HOUSE_MAX_DELTA = 4

# Candidate persistence for reviewer context.
TOP_CANDIDATES_MAX = 3
TOP_CANDIDATES_MIN_PREFILTER = 4

SCORING_MODE_WEIGHTED = "weighted_blend"
SCORING_MODE_ADDRESS_EXACT = "address_exact_override"

SF_OBJECT_NAME = "Site__c"
SF_LAT_FIELD = "Site_Latitude__c"
SF_LNG_FIELD = "Site_Longitude__c"
SF_ZIP_FIELD = "Site_Zip_Code__c"
SF_ADDRESS_FIELD = "Site_Address__c"
SF_CITY_FIELD = "Site_City__c"
SF_STATE_FIELD = "Site_State__c"
