"""CSV output helpers for enrichment runs."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Sequence

DETAIL_COLUMNS: tuple[str, ...] = (
    "Id",
    "sf_lat",
    "sf_lng",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "Site_Zip_Code__c",
    "Stage__c",
    "Owner__c",
    "Carrier_Leasing_Source__c",
    "match_source",
    "match_distance_m",
    "match_record_id",
    "match_asr_number",
    "match_asset_type",
    "classify_lat",
    "classify_lng",
    "naip_site_type",
    "naip_tower_subtype",
    "naip_site_confidence",
    "naip_cell_equipment",
    "asset_lat",
    "asset_lon",
    "asset_offset_m",
    "bucket",
    "holdout_reason",
    "update_lat",
    "update_lng",
    "update_coord_source",
    "update_site_type",
    "update_verified_site",
    "update_verified_site_source",
    "error",
)

CANDIDATE_COLUMNS: tuple[str, ...] = (
    "Id",
    "match_source",
    "match_distance_m",
    "update_lat",
    "update_lng",
    "update_coord_source",
    "update_site_type",
    "update_verified_site",
    "update_verified_site_source",
    "naip_site_type",
    "naip_tower_subtype",
    "naip_site_confidence",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "Site_Zip_Code__c",
)

HOLDOUT_COLUMNS: tuple[str, ...] = (
    "Id",
    "bucket",
    "holdout_reason",
    "match_source",
    "match_distance_m",
    "naip_site_type",
    "naip_tower_subtype",
    "naip_site_confidence",
    "naip_cell_equipment",
    "classify_lat",
    "classify_lng",
    "asset_lat",
    "asset_lon",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "Site_Zip_Code__c",
)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    materialised = list(rows)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in materialised:
            writer.writerow({col: row.get(col, "") for col in columns})
