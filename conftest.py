"""Keep unit tests hermetic now that the end-to-end flow turns network features on by default.

Tests that exercise a feature set its env var themselves (patch.dict), which
overrides these.
"""

import os

_OFFLINE_DEFAULTS = {
    "FOOTPRINT_PIN_CHECK": "0",   # SQL dbo.OvertureBuilding
    "SIGNALS": "0",               # ULS (SQL), OpenCelliD API, OSM
    "SUPPLEMENTAL_IMAGERY": "none",  # Mapillary API
    "SAVED_NEARMAP_CHIPS": "0",   # reads real run folders
    "NEARMAP_CACHE_ONLY": "0",
    "CONFIRM_CONSISTENCY": "0",
}
for key, value in _OFFLINE_DEFAULTS.items():
    os.environ.setdefault(key, value)
