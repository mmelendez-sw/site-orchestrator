"""Milwaukee open-data permit source constants."""

TELECOM_KEYWORDS = [
    "antenna", "antennas", "cell tower", "cell site", "small cell",
    "distributed antenna", "wireless facility", "wireless communication",
    "telecom", "telecommunications", "T-Mobile", "Verizon Wireless",
    "Crown Castle", "American Tower", "SBA Communications", "5G",
    "DAS system", "rooftop carrier", "co-locate", "collocate", "monopole",
    "cellular", "cell", "wireless", "transmission tower",
]

PERMIT_CSV_URL = (
    "https://data.milwaukee.gov/dataset/9bada2e0-fad5-4545-8674-1b2c8c4e9f2f/resource/"
    "828e9630-d7cb-42e4-960e-964eae916397/download/buildingpermits.csv"
)

MPROP_API_URL = "https://data.milwaukee.gov/api/3/action/datastore_search"
MPROP_RESOURCE_ID = "0a2c7f31-cd15-4151-8222-09dd57d5f16d"

SOURCE_NAME = "milwaukee_permits"
SOURCE_URL = "https://data.milwaukee.gov/dataset/building-permits"
