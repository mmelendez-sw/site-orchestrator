"""NAIP-only classification wrapper for enrichment (no Nearmap)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def classify_naip_only(
    *,
    site_id: str,
    lat: float,
    lon: float,
    chip_dir: Path | None = None,
    input_confidence: str = "medium",
    verbose: bool = True,
) -> dict[str, Any]:
    """Classify one coordinate using NAIP imagery only.

    Forces Nearmap off for this call regardless of ambient NAIP_ONLY env.
    Reuses classifier.asset_classifier helpers without modifying orchestrator.
    """
    from enrichment import progress

    # Ensure Nearmap path is skipped even if env has NAIP_ONLY=0.
    os.environ["NAIP_ONLY"] = "1"

    from google import genai

    from classifier import asset_classifier as ac

    # Refresh module flags after env mutation.
    ac.NAIP_ONLY = True
    ac.QUIET = True

    # This enrichment workflow is intentionally Gemini-only. Do not inherit
    # BIFURCATED_AI/Claude escalation settings from the general classifier.
    primary_provider = "gemini"
    allow_claude_escalation = False
    clients: dict[str, object] = {}
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY is required for NAIP classification")
    clients["gemini"] = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    if verbose:
        progress.step("NAIP fetch")
    img, naip_meta, naip_geo = ac.fetch_chip(lat, lon)
    if img is None:
        if verbose:
            progress.warn("No NAIP imagery")
        return {
            "site_type": "no_imagery",
            "error": "no_naip_imagery",
            "lat": lat,
            "lon": lon,
        }

    if chip_dir is not None:
        chip_dir.mkdir(parents=True, exist_ok=True)
        chip_path = chip_dir / f"{site_id}_NAIP.jpg"
        img.save(chip_path, quality=90)
    else:
        chip_path = None

    row = {"id": site_id, "input_confidence": input_confidence}
    prompt = ac.build_classification_prompt(row)
    views = [(ac._naip_view_label(ac.CHIP_SIZE_M), img)]

    if verbose:
        progress.step("Gemini classify")
    # Wide-AOI retry when primary is other/unclear (NAIP-only path).
    res, primary_model, escalation_model, escalation_reason_str = (
        ac.classify_with_routing(
            primary_provider,
            clients,
            views,
            prompt,
            input_confidence,
            escalate=False,
        )
    )
    if verbose:
        progress.result(
            f"{res.get('site_type')} conf={res.get('site_confidence')}"
        )
    classification_stage = "primary"
    active_geo = naip_geo
    active_meta = naip_meta or {}
    naip_chip_m = active_meta.get("naip_chip_m") or ac.CHIP_SIZE_M

    if (
        ac.WIDE_AOI_STAGE
        and res.get("site_type") in ("other", "unclear")
        and ac.NAIP_WIDE_CHIP_M > ac.CHIP_SIZE_M
    ):
        if verbose:
            progress.step("Wide NAIP retry")
        wide_img, wide_meta, wide_geo = ac.fetch_chip(lat, lon, chip_m=ac.NAIP_WIDE_CHIP_M)
        if wide_img is not None:
            wide_views = [(ac._naip_view_label(ac.NAIP_WIDE_CHIP_M), wide_img)]
            wide_res, _, _, _ = ac.classify_with_routing(
                primary_provider,
                clients,
                wide_views,
                prompt,
                input_confidence,
                escalate=False,
            )
            prior_conf = res.get("site_confidence") or 0
            wide_conf = wide_res.get("site_confidence") or 0
            if (
                wide_res.get("site_type") in ac._positive_site_types()
                or wide_conf > prior_conf
            ):
                res = wide_res
                views = wide_views
                active_geo = wide_geo
                active_meta = {**active_meta, **(wide_meta or {})}
                naip_chip_m = ac.NAIP_WIDE_CHIP_M
                classification_stage = "naip_wide"
                if chip_dir is not None:
                    wide_path = chip_dir / f"{site_id}_NAIP_wide.jpg"
                    wide_img.save(wide_path, quality=90)
                    chip_path = wide_path
                if verbose:
                    progress.result(
                        f"wide → {res.get('site_type')} conf={res.get('site_confidence')}"
                    )

    if allow_claude_escalation:
        res, escalation_model, escalation_reason_str = ac.maybe_escalate_to_claude(
            res, clients, views, prompt, input_confidence, allow=True
        )

    asset_lat = asset_lon = asset_offset_m = None
    box, box_view = res.get("asset_box_2d"), res.get("asset_view")
    if box and ac._is_naip_view(box_view) and active_geo:
        located = ac.box_to_latlon(active_geo, box)
        if located:
            asset_lat, asset_lon, asset_offset_m = located

    delay = ac.GEMINI_DELAY_S if primary_provider == "gemini" else ac.API_DELAY_S
    if verbose and delay:
        progress.step(f"pause {delay:g}s")
    time.sleep(delay)

    return {
        "lat": lat,
        "lon": lon,
        "site_type": res.get("site_type"),
        "tower_subtype": res.get("tower_subtype"),
        "site_confidence": res.get("site_confidence"),
        "site_evidence": res.get("site_evidence"),
        "cell_equipment": res.get("cell_equipment"),
        "cell_equipment_confidence": res.get("cell_equipment_confidence"),
        "cell_equipment_evidence": res.get("cell_equipment_evidence"),
        "asset_lat": asset_lat,
        "asset_lon": asset_lon,
        "asset_offset_m": (
            round(asset_offset_m, 1) if asset_offset_m is not None else None
        ),
        "asset_box_2d": json.dumps(box) if box else None,
        "asset_view": box_view,
        "classification_stage": classification_stage,
        "primary_model": primary_model,
        "escalation_model": escalation_model,
        "escalation_reason": escalation_reason_str,
        "naip_chip_m": naip_chip_m,
        "image_date": active_meta.get("image_date"),
        "naip_year": active_meta.get("naip_year"),
        "chip_path": str(chip_path) if chip_path else None,
        "error": None,
    }
