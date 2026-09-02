"""CSV output helpers for enrichment runs."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

_RUN_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
    "Metro_Classification__c",
    "match_source",
    "match_distance_m",
    "match_selection_reason",
    "match_candidate_count",
    "match_runner_up_gap_m",
    "match_record_id",
    "match_asr_number",
    "match_asset_type",
    "classify_lat",
    "classify_lng",
    "classify_coord_source",
    "address_query",
    "address_lat",
    "address_lng",
    "address_geocode_source",
    "address_matched",
    "pin_address_offset_m",
    "pin_address_mismatch",
    "naip_site_type",
    "naip_tower_subtype",
    "naip_site_confidence",
    "naip_cell_equipment",
    "cell_equipment_confidence",
    "cell_equipment_evidence",
    "cell_gear_kind",
    "site_evidence",
    "gemini_cell_equipment",
    "claude_cell_equipment",
    "cell_models_agree",
    "dual_model_resolution",
    "classification_stage",
    "nearmap_tier",
    "nearmap_views",
    "imagery_used",
    "primary_model",
    "escalation_model",
    "escalation_reason",
    "naip_screen_site_type",
    "naip_screen_site_confidence",
    "naip_screen_cell_equipment",
    "second_nearmap",
    "outcome_class",
    "asset_lat",
    "asset_lon",
    "asset_offset_m",
    "asset_coord_source",
    "asset_box_2d",
    "asset_view",
    "bucket",
    "holdout_reason",
    "update_lat",
    "update_lng",
    "update_coord_source",
    "update_site_type",
    "update_verified_site",
    "update_verified_site_source",
    "sf_update_status",
    "sf_update_error",
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
    "sf_update_status",
    "sf_update_error",
    "naip_site_type",
    "naip_tower_subtype",
    "naip_site_confidence",
    "nearmap_tier",
    "imagery_used",
    "primary_model",
    "escalation_model",
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
    "nearmap_tier",
    "imagery_used",
    "primary_model",
    "escalation_model",
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


def expand_run_specs(specs: Sequence[str] | None, *, runs_root: Path) -> list[Path]:
    """Resolve run-folder names or ``YYYY-MM-DD`` prefixes to existing directories.

    Date prefixes expand to matching folders under ``runs_root``, newest name
    first so later chip reuse prefers the latest JPEGs.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in specs or []:
        text = str(raw or "").strip()
        if not text:
            continue
        if _RUN_DATE_PREFIX.match(text):
            matches = sorted(
                (path for path in runs_root.glob(f"{text}*") if path.is_dir()),
                key=lambda path: path.name,
                reverse=True,
            )
            if not matches:
                raise FileNotFoundError(
                    f"No run folders matching {text}* under {runs_root}"
                )
            candidates = matches
        else:
            path = Path(text)
            if not path.is_absolute():
                path = runs_root / text
            if not path.is_dir():
                raise FileNotFoundError(f"Run folder not found: {path}")
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(resolved)
    return found


def holdout_ids_from_run_specs(specs: Sequence[str], *, runs_root: Path) -> list[str]:
    """Load unique Salesforce Ids from holdout CSVs of one or more run folders.

    Each spec is a run directory name under ``runs_root``, a ``YYYY-MM-DD``
    prefix, or a path to a directory / holdout CSV. ``IDS=`` can then
    re-classify dequeued holdouts (the by-Id query ignores LLM_Holdout).
    """
    from enrichment.constants import HOLDOUT_CSV

    seen: set[str] = set()
    ordered: list[str] = []
    csv_paths: list[Path] = []
    leftover: list[str] = []
    for raw in specs:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute() and not _RUN_DATE_PREFIX.match(text):
            path = runs_root / text
        if path.is_file():
            csv_paths.append(path)
        else:
            leftover.append(text)
    for run_dir in expand_run_specs(leftover, runs_root=runs_root) if leftover else []:
        holdout = run_dir / HOLDOUT_CSV
        if holdout.is_file():
            csv_paths.append(holdout)
    for path in csv_paths:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                sf_id = str(row.get("Id") or row.get("sf_id") or "").strip()
                if sf_id and sf_id not in seen:
                    seen.add(sf_id)
                    ordered.append(sf_id)
    return ordered


def site_ids_from_run_specs(
    specs: Sequence[str],
    *,
    runs_root: Path,
    stages: Sequence[str] | None = None,
) -> list[str]:
    """Load unique Salesforce Ids from enrichment_detail.csv of prior runs.

    Defaults to Outreach - Verified. Newest matching run folders are read
    first; a site classified in multiple runs is kept once.
    """
    from enrichment.constants import DEFAULT_STAGE_FILTER, DETAIL_CSV

    stage_set = {
        str(stage).strip()
        for stage in (DEFAULT_STAGE_FILTER if stages is None else stages)
        if str(stage).strip()
    }
    seen: set[str] = set()
    ordered: list[str] = []
    for run_dir in expand_run_specs(specs, runs_root=runs_root):
        path = run_dir / DETAIL_CSV
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if stage_set:
                    stage = str(row.get("Stage__c") or "").strip()
                    if stage not in stage_set:
                        continue
                sf_id = str(row.get("Id") or row.get("sf_id") or "").strip()
                if sf_id and sf_id not in seen:
                    seen.add(sf_id)
                    ordered.append(sf_id)
    return ordered
