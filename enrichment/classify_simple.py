"""Enrichment classify: NAIP + Nearmap + Gemini/Claude, with OSM prefilter.

`classify_site_simple` is a compatibility wrapper around `classify_site_imagery`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def stamp_gemini_tower_solo(res: dict[str, Any]) -> dict[str, Any]:
    """Mark a high-conf Gemini tower so SF apply can write without Claude."""
    from classifier import asset_classifier as ac

    ac.normalize_model_result(res)
    if ac.should_skip_claude_for_gemini_tower(res):
        res["claude_cell_equipment"] = None
        res["cell_models_agree"] = True
        res["dual_model_resolution"] = "gemini_strong_solo"
        res["gemini_cell_equipment"] = res.get("cell_equipment")
    return res


def classify_site_simple(
    *,
    site_id: str,
    lat: float,
    lon: float,
    chip_dir: Path | None = None,
    input_confidence: str = "medium",
    verbose: bool = True,
    pin_lat: float | None = None,
    pin_lon: float | None = None,
    address_lat: float | None = None,
    address_lon: float | None = None,
    pin_address_offset_m: float | None = None,
    pin_address_mismatch: bool = False,
    db_backed: bool = False,
) -> dict[str, Any]:
    """Full imagery classify (Nearmap, Claude, OSM). Same kwargs as the pipeline."""
    from enrichment.naip_classify import classify_site_imagery

    return classify_site_imagery(
        site_id=site_id,
        lat=lat,
        lon=lon,
        chip_dir=chip_dir,
        input_confidence=input_confidence,
        verbose=verbose,
        pin_lat=pin_lat,
        pin_lon=pin_lon,
        address_lat=address_lat,
        address_lon=address_lon,
        pin_address_offset_m=pin_address_offset_m,
        pin_address_mismatch=pin_address_mismatch,
        db_backed=db_backed,
    )
