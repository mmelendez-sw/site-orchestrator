"""Keep unit tests hermetic: no paid APIs, Salesforce, Azure SQL or real run folders.

These defaults are set before any test module imports the code, so
``load_dotenv`` (which never overrides an existing variable) cannot fill in
real credentials from ``.env``. Tests that exercise a feature set its env var
themselves (patch.dict), which overrides these.
"""

import os

_OFFLINE_DEFAULTS = {
    "FOOTPRINT_PIN_CHECK": "0",   # SQL dbo.OvertureBuilding
    "SIGNALS": "0",               # ULS (SQL), OpenCelliD API, OSM
    "SUPPLEMENTAL_IMAGERY": "none",  # Mapillary API
    "SAVED_NEARMAP_CHIPS": "0",   # reads real run folders
    "NEARMAP_CACHE_ONLY": "0",
    "CONFIRM_CONSISTENCY": "0",
    "METRICS_SQL": "0",           # Azure SQL KPI upserts
    "AZURE_SQL_SERVER": "",       # registry / footprint / ULS lookups
    # Blank credentials: a test that forgets a mock fails instead of spending.
    "GEMINI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "NEARMAP_API_KEY": "",
    "MAPILLARY_ACCESS_TOKEN": "",
    "OPENCELLID_API_KEY": "",
    "SF_PASSWORD": "",
    "SF_SECURITY_TOKEN": "",
}
for key, value in _OFFLINE_DEFAULTS.items():
    os.environ.setdefault(key, value)
