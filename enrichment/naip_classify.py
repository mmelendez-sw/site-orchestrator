"""Full imagery classification for enrichment (Nearmap + bifurcated AI).

Honors classifier env flags from `.env` / process env:
  NEARMAP_TIERED, BIFURCATED_AI, GEMINI_ONLY, NAIP_ONLY, ZOOM_STAGE, WIDE_AOI_STAGE

`classify_naip_only` remains as an explicit NAIP/Gemini-only escape hatch.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _refresh_classifier_flags(ac: Any) -> None:
    """Re-bind module flags from current env (enrichment may import after .env load)."""
    ac.NAIP_ONLY = ac._env_flag("NAIP_ONLY", default="0")
    ac.NEARMAP_TIERED = ac._env_flag("NEARMAP_TIERED", default="1")
    ac.BIFURCATED_AI = ac._env_flag("BIFURCATED_AI", default="1")
    ac.GEMINI_ONLY = ac._env_flag("GEMINI_ONLY", default="0")
    ac.TOWER_ONLY = ac._env_flag("TOWER_ONLY", default="0")
    ac.ZOOM_STAGE = ac._env_flag("ZOOM_STAGE", default="1")
    ac.WIDE_AOI_STAGE = ac._env_flag("WIDE_AOI_STAGE", default="1")
    ac.NEARMAP_API_KEY = (os.environ.get("NEARMAP_API_KEY") or "").strip()
    try:
        ac.NAIP_MAX_AGE_YEARS = float(os.environ.get("NAIP_MAX_AGE_YEARS", "2"))
    except ValueError:
        ac.NAIP_MAX_AGE_YEARS = 2.0
    try:
        ac.NAIP_AGE_HIGH_CONF_OVERRIDE = float(
            os.environ.get("NAIP_AGE_HIGH_CONF_OVERRIDE", "0.85")
        )
    except ValueError:
        ac.NAIP_AGE_HIGH_CONF_OVERRIDE = 0.85


def _build_clients(ac: Any, primary_provider: str, allow_claude: bool) -> dict[str, object]:
    from google import genai
    from anthropic import Anthropic

    clients: dict[str, object] = {}
    if primary_provider == "gemini" or allow_claude:
        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError("GEMINI_API_KEY is required for enrichment classification")
        clients["gemini"] = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    if primary_provider == "claude" or allow_claude:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required when BIFURCATED_AI=1 or Claude-primary"
            )
        clients["claude"] = Anthropic()
    return clients


def classify_site_imagery(
    *,
    site_id: str,
    lat: float,
    lon: float,
    chip_dir: Path | None = None,
    input_confidence: str = "medium",
    verbose: bool = True,
    pin_lat: float | None = None,
    pin_lon: float | None = None,
    pin_address_offset_m: float | None = None,
    pin_address_mismatch: bool = False,
    db_backed: bool = False,
) -> dict[str, Any]:
    """Classify one coordinate with the full Nearmap + bifurcated AI stack.

    Same strategy as `classifier.asset_classifier` main loop for a single pin:
    NAIP → (optional) Nearmap vert/obliques → wide AOI / zoom → Claude escalate.

    ``db_backed``: FCC/TowerSource already matched. High-conf Gemini towers
    then skip Vert/obliques. Imagery-only tower+cell still fetches obliques.

    When pin_address_mismatch is set, also save a pin-centered Nearmap Vert chip
    (`{id}_pin_nearmap_vert.jpg`) so review can compare address vs SF pin.
    """
    from enrichment import progress
    from classifier import asset_classifier as ac

    _refresh_classifier_flags(ac)
    ac.QUIET = True
    if chip_dir is not None:
        chip_dir = Path(chip_dir)
        chip_dir.mkdir(parents=True, exist_ok=True)
        ac.CHIP_DIR = chip_dir

    primary_provider, allow_claude_escalation = ac.resolve_ai_mode()
    clients = _build_clients(ac, primary_provider, allow_claude_escalation)

    if verbose:
        mode = (
            f"Nearmap={'off' if ac.NAIP_ONLY else ('tiered' if ac.NEARMAP_TIERED else 'on')}"
            f" | AI={primary_provider}"
            f"{'+Claude' if allow_claude_escalation else ''}"
        )
        progress.step(mode)

    if verbose:
        progress.step("NAIP fetch")
    img, naip_meta, naip_geo = ac.fetch_chip(lat, lon)
    img_date = (naip_meta or {}).get("image_date")
    naip_chip_m = (naip_meta or {}).get("naip_chip_m") or ac.CHIP_SIZE_M

    skip_paid_imagery = bool(ac.NAIP_ONLY)
    if not skip_paid_imagery:
        try:
            from enrichment.osm_prefilter import (
                lookup_osm_features,
                osm_suggests_empty_chip,
            )

            osm_info = lookup_osm_features(lat, lon)
            if osm_info.get("communication_tower"):
                db_backed = True
                if verbose:
                    progress.result("OSM communication tower nearby")
            if osm_suggests_empty_chip(osm_info) and not db_backed:
                skip_paid_imagery = True
                if verbose:
                    progress.result("OSM: no building/tower — skip Nearmap")
        except Exception as exc:  # noqa: BLE001
            if verbose:
                progress.warn(f"OSM prefilter failed: {exc}")

    nearmap_views: dict = {}
    nearmap_date = None
    if not skip_paid_imagery and not ac.NEARMAP_TIERED:
        try:
            nearmap_views, nearmap_date = ac.fetch_nearmap_views(lat, lon)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                progress.warn(f"Nearmap fetch failed: {exc}")

    if img is None and not nearmap_views and not (
        not skip_paid_imagery and ac.NEARMAP_TIERED and ac.NEARMAP_API_KEY
    ):
        if verbose:
            progress.warn("No imagery")
        return {
            "site_type": "no_imagery",
            "error": "no_imagery",
            "lat": lat,
            "lon": lon,
        }

    row = {"id": site_id, "input_confidence": input_confidence}
    prompt = ac.build_classification_prompt(row)
    input_conf = ac.normalize_input_confidence(input_confidence)

    def build_views(nm_views, naip_img=None, chip_m=None):
        views = []
        source_img = img if naip_img is None else naip_img
        side_m = naip_chip_m if chip_m is None else chip_m
        if source_img is not None:
            views.append((ac._naip_view_label(side_m), source_img))
        for name, vimg in nm_views.items():
            if chip_dir is not None:
                vpath = chip_dir / f"{site_id}_nearmap_{name.lower()}.jpg"
                vimg.save(vpath, quality=90)
            label = (
                "Nearmap top-down"
                if name == "Vert"
                else f"Nearmap oblique ({name})"
            )
            views.append((label, vimg))
        return views

    chip_path = None
    if img is not None and chip_dir is not None:
        chip_path = chip_dir / f"{site_id}_NAIP.jpg"
        img.save(chip_path, quality=90)

    primary_model = primary_provider
    escalation_model = None
    escalation_reason_str = None
    classification_stage = "primary"
    zoom_count = 0
    nearmap_tier = "naip_only"
    views: list = []

    if skip_paid_imagery:
        if img is None:
            return {
                "site_type": "no_imagery",
                "error": "no_naip_imagery",
                "lat": lat,
                "lon": lon,
            }
        views = build_views({})
        if verbose:
            progress.step("Gemini classify (NAIP)")
        res, primary_model, escalation_model, escalation_reason_str = (
            ac.classify_with_routing(
                primary_provider,
                clients,
                views,
                prompt,
                input_conf,
                escalate=False,
                screen=True,
            )
        )
        nearmap_tier = "naip_only"
    elif ac.NEARMAP_TIERED:
        if verbose:
            progress.step("Tiered Nearmap classify")
        res, nearmap_views, nearmap_date, nearmap_tier, views = ac.classify_with_tiers(
            lat,
            lon,
            img,
            primary_provider,
            clients,
            prompt,
            input_conf,
            build_views,
            naip_age_years=(naip_meta or {}).get("image_age_years"),
            db_backed=db_backed,
        )
        primary_model = primary_provider
    else:
        if verbose:
            progress.step("classify")
        views = build_views(nearmap_views)
        res, primary_model, escalation_model, escalation_reason_str = (
            ac.classify_with_routing(
                primary_provider,
                clients,
                views,
                prompt,
                input_conf,
                escalate=False,
            )
        )
        nearmap_tier = "full" if nearmap_views else "naip_only"

    if verbose:
        progress.result(
            f"{res.get('site_type')} conf={res.get('site_confidence')} tier={nearmap_tier}"
        )

    nearmap_aoi_m = ac.NEARMAP_CHIP_M if nearmap_views else None
    stage_provider = primary_provider

    has_obliques = any(name != "Vert" for name in nearmap_views)
    nearmap_settled_other = (
        nearmap_tier == "full"
        and has_obliques
        and res.get("site_type") in ("other", "unclear")
    )
    nearmap_blocks_rescue = ac.nearmap_full_blocks_rescue(
        res, nearmap_tier=nearmap_tier, has_obliques=has_obliques
    )
    pin_offset_scout = (
        not nearmap_blocks_rescue and ac.needs_pin_offset_scout(res)
    )
    naip_rescue = (
        not nearmap_blocks_rescue
        and not pin_offset_scout
        and ac.needs_naip_rescue(res)
    )
    # Snapshot before wide/zoom so a rejected re-center can restore a weak rooftop.
    pre_scout_res = dict(res)
    if ac.confident_no_asset(res) and verbose:
        progress.result("Confident NAIP other — skip wide/zoom/Nearmap rescue")
    elif nearmap_blocks_rescue and verbose:
        progress.result(
            "Nearmap full+obliques: locked HVAC rooftop — skip wide/zoom rescue"
        )
    elif pin_offset_scout and verbose:
        progress.result(
            "Rooftop pin may be offset — Nearmap/NAIP scout allowed "
            f"(cell={res.get('cell_equipment')!r})"
        )
    elif naip_rescue and verbose:
        progress.result(
            "Weak NAIP other/unclear — NAIP wide/zoom only "
            f"(conf={res.get('site_confidence')})"
        )

    # Wide Nearmap AOI — rooftops only (not empty-field hunting).
    if (
        pin_offset_scout
        and not skip_paid_imagery
        and ac.NEARMAP_API_KEY
    ):
        try:
            if verbose:
                progress.step(f"Nearmap wide AOI ({ac.NEARMAP_FALLBACK_CHIP_M}m)")
            wide_views, wide_date = ac.fetch_nearmap_views(
                lat, lon, ac.NEARMAP_FALLBACK_CHIP_M
            )
        except Exception as exc:  # noqa: BLE001
            wide_views, wide_date = {}, None
            if verbose:
                progress.warn(f"wide Nearmap failed: {exc}")
        if wide_views:
            nearmap_views = wide_views
            nearmap_date = wide_date or nearmap_date
            nearmap_aoi_m = ac.NEARMAP_FALLBACK_CHIP_M
            views = build_views(wide_views)
            res, _, _, _ = ac.classify_with_routing(
                stage_provider,
                clients,
                views,
                prompt,
                input_conf,
                escalate=False,
            )
            classification_stage = "wide_aoi"
            nearmap_tier = "wide_aoi"
            if verbose:
                progress.result(
                    f"wide → {res.get('site_type')} conf={res.get('site_confidence')}"
                )

    # NAIP wide chip retry — rooftop pin-offset or weak other/unclear.
    wide_was_other = False
    if (
        (pin_offset_scout or naip_rescue)
        and not ac.confident_no_asset(res)
        and ac.WIDE_AOI_STAGE
        and img is not None
        and ac.NAIP_WIDE_CHIP_M > ac.CHIP_SIZE_M
        and res.get("site_type") in ("other", "unclear", "rooftop")
        and res.get("cell_equipment") is not True
    ):
        if verbose:
            progress.step(f"NAIP wide ({int(ac.NAIP_WIDE_CHIP_M)}m)")
        wide_img, wide_meta, wide_geo = ac.fetch_chip(
            lat, lon, chip_m=ac.NAIP_WIDE_CHIP_M
        )
        if wide_img is not None:
            views = build_views(
                nearmap_views, naip_img=wide_img, chip_m=ac.NAIP_WIDE_CHIP_M
            )
            wide_res, _, _, _ = ac.classify_with_routing(
                stage_provider,
                clients,
                views,
                prompt,
                input_conf,
                escalate=False,
            )
            prior_conf = res.get("site_confidence") or 0
            wide_conf = wide_res.get("site_confidence") or 0
            if (
                wide_res.get("site_type") in ac._positive_site_types()
                or wide_conf > prior_conf
            ):
                res = wide_res
                img = wide_img
                naip_geo = wide_geo
                naip_chip_m = ac.NAIP_WIDE_CHIP_M
                if wide_meta:
                    naip_meta = {**(naip_meta or {}), **wide_meta}
                    img_date = wide_meta.get("image_date") or img_date
                if chip_dir is not None:
                    chip_path = chip_dir / f"{site_id}_NAIP_wide.jpg"
                    wide_img.save(chip_path, quality=90)
                classification_stage = "wide_aoi"
                nearmap_tier = "naip_wide"
                if verbose:
                    progress.result(
                        f"naip-wide → {res.get('site_type')} conf={res.get('site_confidence')}"
                    )
            if str(wide_res.get("site_type") or "").strip().lower() == "other":
                wide_was_other = True

    # Zoom stage — skip when wide AOI also returned other.
    if (
        (pin_offset_scout or naip_rescue)
        and not ac.confident_no_asset(res)
        and not wide_was_other
        and ac.ZOOM_STAGE
        and res.get("site_type") in ("other", "unclear", "rooftop")
        and res.get("cell_equipment") is not True
    ):
        source_label, source_img = None, None
        if nearmap_views.get("Vert"):
            source_label, source_img = "Nearmap top-down", nearmap_views["Vert"]
        elif img is not None:
            source_label, source_img = "NAIP top-down", img
        if source_img is not None:
            if verbose:
                progress.step("zoom scout")
            zoom_res, zoom_count = ac.run_zoom_stage(
                stage_provider,
                clients,
                site_id,
                views,
                source_label,
                source_img,
                max_crops=ac.ZOOM_MAX_CANDIDATES,
            )
            prior_conf = res.get("site_confidence") or 0
            zoom_conf = zoom_res.get("site_confidence") or 0
            if (
                zoom_res.get("site_type") in ac._positive_site_types()
                or zoom_conf > prior_conf
            ):
                res = zoom_res
                classification_stage = "zoom"
                nearmap_tier = "zoom"
                if verbose:
                    progress.result(
                        f"zoom → {res.get('site_type')} conf={res.get('site_confidence')}"
                    )

    # Pin-offset rescue: if pin-centered Nearmap was weak/other but wide/zoom
    # found a positive, re-center Nearmap on the asset box and require confirmation.
    pin_centered_weak = nearmap_settled_other or (
        str(pre_scout_res.get("site_type") or "").lower() == "rooftop"
        and pre_scout_res.get("cell_equipment") is not True
        and not nearmap_blocks_rescue
    )
    scout_found_cell = (
        res.get("site_type") in ac._positive_site_types()
        and res.get("cell_equipment") is True
        and (
            classification_stage in {"wide_aoi", "zoom"}
            or nearmap_tier in {"wide_aoi", "naip_wide", "zoom"}
        )
    )
    if (
        pin_centered_weak
        and scout_found_cell
        and not skip_paid_imagery
        and ac.NEARMAP_API_KEY
    ):
        box = res.get("asset_box_2d")
        if isinstance(box, str):
            try:
                box = json.loads(box)
            except json.JSONDecodeError:
                box = None
        located = None
        box_view = res.get("asset_view")
        if box and ac._is_naip_view(box_view) and naip_geo:
            located = ac.box_to_latlon(naip_geo, box)
        elif box and box_view and "top-down" in str(box_view).lower():
            chip_m = float(nearmap_aoi_m or ac.NEARMAP_CHIP_M)
            located = ac.box_to_latlon_centered(lat, lon, chip_m, box)
        if located:
            cand_lat, cand_lon, cand_offset = located
            if cand_offset >= 20:
                if verbose:
                    progress.step(
                        f"re-center Nearmap ({cand_offset:.0f}m from pin)"
                    )
                try:
                    recenter_views, recenter_date = ac.fetch_nearmap_views(
                        cand_lat, cand_lon
                    )
                except Exception as exc:  # noqa: BLE001
                    recenter_views, recenter_date = {}, None
                    if verbose:
                        progress.warn(f"re-center Nearmap failed: {exc}")
                if recenter_views:
                    views = build_views(recenter_views)
                    check, _, _, _ = ac.classify_with_routing(
                        stage_provider,
                        clients,
                        views,
                        prompt,
                        input_conf,
                        escalate=False,
                    )
                    ok = (
                        check.get("site_type") in ac._positive_site_types()
                        and check.get("cell_equipment") is True
                    )
                    if ok:
                        res = check
                        lat, lon = cand_lat, cand_lon
                        nearmap_views = recenter_views
                        nearmap_date = recenter_date or nearmap_date
                        nearmap_aoi_m = ac.NEARMAP_CHIP_M
                        has_obliques = any(n != "Vert" for n in nearmap_views)
                        classification_stage = "pin_recenter"
                        nearmap_tier = "full" if has_obliques else "vert_only"
                        if verbose:
                            progress.result(
                                f"re-center confirmed → {res.get('site_type')} "
                                f"cell={res.get('cell_equipment')}"
                            )
                    else:
                        # Restore pin-centered result (weak rooftop) rather than
                        # forcing other when re-center rejects a wide scout hit.
                        res = dict(pre_scout_res)
                        classification_stage = "pin_recenter_rejected"
                        if verbose:
                            progress.result(
                                "re-center rejected — keep pin-centered result"
                            )

    # Stamp imagery context before escalation / dual-model.
    res["nearmap_tier"] = nearmap_tier
    res["nearmap_views"] = ",".join(nearmap_views) if nearmap_views else None
    res["classification_stage"] = classification_stage
    res["gemini_pre_escalation_cell"] = res.get("cell_equipment")
    res["gemini_pre_escalation_cell_conf"] = res.get("cell_equipment_confidence")
    res["gemini_pre_escalation_evidence"] = res.get("cell_equipment_evidence")
    res["gemini_pre_escalation_gear"] = res.get("cell_gear_kind")
    from_wide_rescue = classification_stage in {
        "wide_aoi",
        "zoom",
        "pin_recenter_rejected",
    } or nearmap_tier in {"naip_wide", "wide_aoi"}
    # Validated pin-recenter is NOT treated as a risky wide rescue.
    if classification_stage == "pin_recenter":
        from_wide_rescue = False

    # Claude escalation after imagery stages.
    if allow_claude_escalation:
        if verbose:
            progress.step("Claude escalate?")
        res, escalation_model, escalation_reason_str = ac.maybe_escalate_to_claude(
            res, clients, views, prompt, input_conf, allow=True
        )
        if verbose and escalation_model:
            progress.result(
                f"escalated ({escalation_reason_str}) → {res.get('site_type')}"
            )

    # Final HVAC false-positive guard for rooftop cell=true.
    res = ac.gate_weak_rooftop_cell_claim(res)
    # Dual-model Haiku/Sonnet covers HVAC FPs; skip a second full-scene recheck.
    res = ac.gate_weak_stealth_tower_claim(res)
    # Repair missing/invalid boxes before crop + dual-model.
    box_provider = primary_provider if not escalation_model else "claude"
    res = ac.maybe_repair_rooftop_asset_box(box_provider, clients, res, views)
    res = ac.gate_weak_rooftop_cell_claim(res)
    res = ac.gate_weak_stealth_tower_claim(res)
    # Dual-model Haiku crop confirm covers the old Gemini crop recheck.
    # No usable box ⇒ cannot keep rooftop cell=true (proof chain incomplete).
    res = ac.enforce_rooftop_cell_requires_box(res, views)
    confirm_views, used_crop = ac.build_cell_confirm_views(res, views)
    if (
        str(res.get("site_type") or "").lower() == "rooftop"
        and res.get("cell_equipment") is True
        and not used_crop
    ):
        # Last resort: do not dual-confirm full-scene when box/crop failed.
        res = ac.enforce_rooftop_cell_requires_box(res, views)
        confirm_views, used_crop = ac.build_cell_confirm_views(res, views)
    if used_crop and verbose:
        progress.step("dual-model on cell crop")
    res, dual_model, cell_agree = ac.confirm_rooftop_cell_with_claude(
        res,
        clients,
        confirm_views,
        already_escalated=bool(escalation_model),
        allow_soft_keep=False,
        from_wide_rescue=from_wide_rescue,
        used_crop=used_crop,
        allow_gemini_solo=False,
        all_views=views,
    )
    if dual_model and not escalation_model:
        escalation_model = dual_model
        escalation_reason_str = escalation_reason_str or (
            "gemini_high_conf_tower"
            if dual_model == "gemini_strong_solo"
            else "rooftop_dual_model_cell"
        )
    res["cell_models_agree"] = cell_agree
    # Soft-keep / agree must still have a box for candidacy confidence.
    if res.get("cell_equipment") is True:
        res = ac.enforce_rooftop_cell_requires_box(res, views)
        if res.get("cell_equipment") is not True:
            cell_agree = False
            res["cell_models_agree"] = False
    res = ac.align_site_evidence_with_cell(res)

    asset_lat = asset_lon = asset_offset_m = None
    asset_coord_source = None
    box, box_view = res.get("asset_box_2d"), res.get("asset_view")
    if box:
        located = ac.locate_asset_box_latlon(
            lat=lat,
            lon=lon,
            box=box,
            box_view=box_view,
            naip_geo=naip_geo,
            nearmap_aoi_m=nearmap_aoi_m,
        )
        if located:
            asset_lat, asset_lon, asset_offset_m, asset_coord_source = located

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
        "cell_gear_kind": res.get("cell_gear_kind"),
        "gemini_cell_equipment": res.get("gemini_cell_equipment"),
        "claude_cell_equipment": res.get("claude_cell_equipment"),
        "cell_models_agree": res.get("cell_models_agree"),
        "dual_model_resolution": res.get("dual_model_resolution") or "",
        "asset_lat": asset_lat,
        "asset_lon": asset_lon,
        "asset_offset_m": (
            round(asset_offset_m, 1) if asset_offset_m is not None else None
        ),
        "asset_coord_source": asset_coord_source or "",
        "asset_box_2d": json.dumps(box) if box else None,
        "asset_view": box_view,
        "classification_stage": classification_stage,
        "nearmap_tier": nearmap_tier,
        "nearmap_date": nearmap_date,
        "nearmap_views": ",".join(nearmap_views) if nearmap_views else None,
        "nearmap_aoi_m": nearmap_aoi_m,
        "zoom_crops": zoom_count or None,
        "primary_model": primary_model,
        "escalation_model": escalation_model,
        "escalation_reason": escalation_reason_str,
        "naip_chip_m": naip_chip_m,
        "image_date": img_date,
        "naip_year": (naip_meta or {}).get("naip_year"),
        "chip_path": str(chip_path) if chip_path else None,
        "error": None,
    }


def classify_naip_only(
    *,
    site_id: str,
    lat: float,
    lon: float,
    chip_dir: Path | None = None,
    input_confidence: str = "medium",
    verbose: bool = True,
) -> dict[str, Any]:
    """Legacy NAIP + Gemini-only path (no Nearmap, no Claude)."""
    prior = {
        "NAIP_ONLY": os.environ.get("NAIP_ONLY"),
        "NEARMAP_TIERED": os.environ.get("NEARMAP_TIERED"),
        "BIFURCATED_AI": os.environ.get("BIFURCATED_AI"),
        "GEMINI_ONLY": os.environ.get("GEMINI_ONLY"),
    }
    try:
        os.environ["NAIP_ONLY"] = "1"
        os.environ["NEARMAP_TIERED"] = "0"
        os.environ["BIFURCATED_AI"] = "0"
        os.environ["GEMINI_ONLY"] = "1"
        return classify_site_imagery(
            site_id=site_id,
            lat=lat,
            lon=lon,
            chip_dir=chip_dir,
            input_confidence=input_confidence,
            verbose=verbose,
        )
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
