"""Site classifier decision logic: which imagery to buy and which model to trust.

One site flows NAIP screen → (skip gates) → Nearmap Vert + obliques →
wide AOI / zoom rescue → Claude escalation → rooftop box repair →
Gemini+Claude dual-model cell confirm. ``enrichment.naip_classify`` drives
that sequence per Salesforce site; this module holds the gates and the
model-call wrappers it composes.

Supporting modules:
  classifier.prompts   prompt text + reply schemas
  classifier.imagery   NAIP / Nearmap fetch (disk-cached)
  classifier.llm       Gemini / Claude transport + shared rate limits
  classifier.views     view labels, boxes, crops
  classifier.evidence  evidence-text keyword cues

Notes:
  - NAIP covers the continental US only (~0.6-1m resolution, public domain).
  - Env flags are read at import and re-read per site by
    ``enrichment.naip_classify._refresh_classifier_flags``.
"""

from __future__ import annotations

import os
from pathlib import Path

from anthropic import Anthropic
from google import genai
from google.genai import types as genai_types
from PIL import Image

from classifier import imagery, llm
from classifier.evidence import (
    SPECULATIVE_STEALTH_CUES,
    evidence_text,
    has_any,
    has_stealth_form,
    has_stealth_hardware,
    has_telecom_cue,
)
from classifier.imagery import OBLIQUE_VIEWS, fetch_nearmap_views
from classifier.llm import _views_to_claude_content, _views_to_gemini_contents
from classifier.prompts import (
    CELL_GEAR_KIND_VALUES,
    CLAIMED_SITE_TOWER_CONFIRM_NOTE,
    CLASSIFICATION_PROMPT,
    EQUIPMENT_RECHECK_PROMPT,
    GEMINI_SCAN_SCHEMA,
    INPUT_CONFIDENCE_PROMPTS,
    ROOFTOP_BOX_REPAIR_PROMPT,
    ROOFTOP_CELL_LOCALIZE_CONFIRM_PROMPT,
    SCAN_PROMPT,
    SCAN_SCHEMA,
    SITE_TYPE_VALUES,
    TOWER_ONLY_CLASSIFICATION_PROMPT,
    TOWER_ONLY_SCAN_PROMPT,
    TOWER_ONLY_SITE_TYPE_VALUES,
    TOWER_ONLY_ZOOM_CLASSIFICATION_PROMPT,
    ZOOM_CLASSIFICATION_PROMPT,
    cell_confirm_prompt,
    classify_schema,
)
from classifier.views import (
    CELL_CONFIRM_PAD_FRAC,
    SCREEN_IMAGE_MAX_PX,
    ZOOM_MAX_CANDIDATES,
    _crop_zoom,
    _grid_boxes,
    _oblique_views_only,
    _valid_box,
    asset_view_is_nearmap_oblique,
    box_iou,
    coerce_asset_box,
    get_valid_asset_box,
    naip_view_label,
    pick_view_for_asset_box,
    trim_views_for_model,
)
from envutil import env_flag, env_float, env_str

# When True (enrichment default), suppress chatter but still emit short
# per-step "— done" progress lines (NAIP, Nearmap, classify, zoom, etc.).
QUIET = False


def _out(msg: str = "", *, important: bool = False) -> None:
    """Print unless QUIET; important=True always prints (stage/result progress)."""
    if QUIET and not important:
        return
    from enrichment.progress import emit

    emit(msg)


def _env_flag(name: str, default: str = "0") -> bool:
    return env_flag(name, default)


# ----------------------------- configuration --------------------------------

NEARMAP_TIERED = _env_flag("NEARMAP_TIERED", default="1")
BIFURCATED_AI = _env_flag("BIFURCATED_AI", default="1")
GEMINI_ONLY = _env_flag("GEMINI_ONLY")
TOWER_ONLY = _env_flag("TOWER_ONLY")
NAIP_ONLY = _env_flag("NAIP_ONLY")
ZOOM_STAGE = _env_flag("ZOOM_STAGE", default="1")
# Widen NAIP AOI and re-classify when primary pass is other/unclear
# (towers just outside the frame). One extra Gemini call — cheaper than zoom.
WIDE_AOI_STAGE = _env_flag("WIDE_AOI_STAGE", default="1")
TIER_CONF_HIGH = env_float("TIER_CONF_HIGH", 0.75)
TIER_CONF_MEDIUM = env_float("TIER_CONF_MEDIUM", 0.6)
# Rooftop cell_equipment_confidence bar for early-stop / Claude skip (matches
# enrichment MIN_ROOFTOP_CELL_CONFIDENCE). Below this → keep fetching / escalate.
ROOFTOP_CELL_CONF_MIN = env_float("ROOFTOP_CELL_CONF_MIN", 0.75)
# Stale NAIP cannot early-stop Nearmap for rooftops (equipment may post-date the chip),
# unless confidence is at/above this override (definitive NAIP-only identification).
NAIP_MAX_AGE_YEARS = env_float("NAIP_MAX_AGE_YEARS", 2)
NAIP_AGE_HIGH_CONF_OVERRIDE = env_float("NAIP_AGE_HIGH_CONF_OVERRIDE", 0.85)
GEMINI_MODEL = env_str("GEMINI_MODEL", "gemini-3-flash-preview")
# Cheap NAIP screen; promote to GEMINI_MODEL for tower/rooftop/unclear/weak other.
GEMINI_SCREEN_MODEL = env_str("GEMINI_SCREEN_MODEL", "gemini-3.5-flash-lite")
# Gemini 3.x thinking_level: NAIP screen LOW; Nearmap/confirm MEDIUM.
_THINKING_LEVELS = frozenset({"MINIMAL", "LOW", "MEDIUM", "HIGH"})


def _parse_thinking_level(raw: str | None, default: str) -> str:
    text = (raw or "").strip().upper()
    return text if text in _THINKING_LEVELS else default


GEMINI_SCREEN_THINKING_LEVEL = _parse_thinking_level(
    os.environ.get("GEMINI_SCREEN_THINKING_LEVEL"), "LOW"
)
GEMINI_THINKING_LEVEL = _parse_thinking_level(
    os.environ.get("GEMINI_THINKING_LEVEL"), "MEDIUM"
)
CLAUDE_ESCALATION_MODEL = env_str("CLAUDE_ESCALATION_MODEL", "claude-sonnet-4-6")
# Dual-model cell crops use Haiku; Sonnet stays on rooftop HVAC localize.
CLAUDE_CROP_MODEL = env_str("CLAUDE_CROP_MODEL", "claude-haiku-4-5-20251001")
# Thinking tokens for Gemini 2.x vision calls. Empty/auto: 0 (thinking is billed).
GEMINI_THINKING_BUDGET_ENV = env_str("GEMINI_THINKING_BUDGET")
# Soft-keep / solo-trust cell confidence floors.
GEMINI_SOFT_KEEP_CELL_CONF = env_float("GEMINI_SOFT_KEEP_CELL_CONF", 0.85)
# Skip Claude (escalation + tower dual-model) when Gemini site_confidence is
# at/above this. Rooftop HVAC FPs still go through Claude dual-confirm.
GEMINI_SOLO_CELL_CONF = env_float("GEMINI_SOLO_CELL_CONF", 0.85)
EMPTY_CHIP_LOCK_CONF = env_float("NEARMAP_EMPTY_LOCK_CONF", 0.90)
# Do not burn Claude on weak Gemini — below this site_confidence, skip
# full-scene escalation and dual-model Claude. Weak calls stay holdout.
# 0.60 matches the NAIP scout floor so imagery-only towers at 0.65 still get Claude.
CLAUDE_ESCALATE_MIN_SITE_CONF = env_float("CLAUDE_ESCALATE_MIN_SITE_CONF", 0.60)
# Min IoU between Gemini and Claude boxes on localize dual-confirm.
GEMINI_CLAUDE_BOX_IOU = env_float("GEMINI_CLAUDE_BOX_IOU", 0.20)
GEMINI_RETRIES = llm.GEMINI_RETRIES

# Primary NAIP chip side length (meters). Set to 500 to skip a prior 250 m pass
# and go straight to wide-frame classify (then ZOOM_STAGE if still other/unclear).
CHIP_SIZE_M = env_float("CHIP_SIZE_M", 300)
# Wide-AOI (zoom-out) retry size when primary NAIP pass finds no tower.
# Only runs when NAIP_WIDE_CHIP_M > CHIP_SIZE_M.
NAIP_WIDE_CHIP_M = env_float("NAIP_WIDE_CHIP_M", 500)
NEARMAP_API_KEY = imagery.nearmap_api_key()
# Buy the first oblique, and the rest only when the call is not yet locked.
NEARMAP_STAGGER_OBLIQUES = _env_flag("NEARMAP_STAGGER_OBLIQUES", default="1")
INPUT_CONFIDENCE_LEVELS = ("high", "medium", "low")
# Per-run chip folder (set by enrichment.naip_classify for each run).
CHIP_DIR = Path("chips")


def fetch_chip(lat: float, lon: float, chip_m: float | None = None):
    """NAIP chip at the current CHIP_SIZE_M unless ``chip_m`` is given."""
    return imagery.fetch_chip(lat, lon, CHIP_SIZE_M if chip_m is None else chip_m)


def _naip_view_label(chip_m: float | None = None) -> str:
    """Label used in the view list and for matching asset_view to geo."""
    return naip_view_label(chip_m, CHIP_SIZE_M)


def _site_type_values() -> tuple[str, ...]:
    return TOWER_ONLY_SITE_TYPE_VALUES if TOWER_ONLY else SITE_TYPE_VALUES


def _positive_site_types() -> tuple[str, ...]:
    if TOWER_ONLY:
        return ("tower",)
    return ("tower", "rooftop")


def _classification_response_schemas() -> tuple[dict, dict]:
    """(Claude, Gemini) reply schemas for the current TOWER_ONLY mode."""
    site_types = _site_type_values()
    return (
        classify_schema(gemini=False, site_types=site_types),
        classify_schema(gemini=True, site_types=site_types),
    )


def resolve_ai_mode() -> tuple[str, bool]:
    """Return (primary_provider, allow_claude_escalation)."""
    if GEMINI_ONLY:
        return "gemini", False
    if BIFURCATED_AI:
        return "gemini", True
    return "claude", False


def _active_scan_prompt() -> str:
    return TOWER_ONLY_SCAN_PROMPT if TOWER_ONLY else SCAN_PROMPT


def _active_zoom_prompt() -> str:
    return TOWER_ONLY_ZOOM_CLASSIFICATION_PROMPT if TOWER_ONLY else ZOOM_CLASSIFICATION_PROMPT


def source_expects_asset(res: dict | None = None, *, input_confidence: str | None = None) -> bool:
    """True when outreach-verified / high source trust says an asset is likely."""
    conf = input_confidence
    if conf is None and res is not None:
        conf = res.get("input_confidence")
    return normalize_input_confidence(conf) == "high"


def normalize_input_confidence(value) -> str:
    """Return high | medium | low. Missing or invalid values default to medium."""
    if value is None or value != value:  # None / NaN
        return "medium"
    level = str(value).strip().lower()
    return level if level in INPUT_CONFIDENCE_LEVELS else "medium"


def normalize_confidence(value, default: float | None = None) -> float | None:
    """Clamp model confidence to 0-1. Values > 1 are treated as wrong-scale output."""
    if value is None or value != value:  # None / NaN
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v > 1.0:
        if v <= 10.0:
            v /= 10.0
        elif v <= 100.0:
            v /= 100.0
        else:
            v = 1.0
    return max(0.0, min(1.0, v))


def normalize_model_result(res: dict) -> dict:
    """Fix site_confidence and cell_equipment_confidence after a model JSON reply."""
    if "site_confidence" in res:
        norm = normalize_confidence(res.get("site_confidence"))
        if norm is not None:
            res["site_confidence"] = norm
    if "cell_equipment_confidence" in res:
        norm = normalize_confidence(res.get("cell_equipment_confidence"))
        if norm is not None:
            res["cell_equipment_confidence"] = norm
    if res.get("site_type") != "tower":
        res["tower_subtype"] = None
    elif res.get("tower_subtype") in ("", "null"):
        res["tower_subtype"] = None
    kind = str(res.get("cell_gear_kind") or "").strip().lower().replace(" ", "_")
    if kind in CELL_GEAR_KIND_VALUES:
        res["cell_gear_kind"] = kind
    elif res.get("cell_equipment") is True:
        res["cell_gear_kind"] = "unclear"
    elif res.get("cell_equipment") is False:
        res["cell_gear_kind"] = "none"
    return res


def build_classification_prompt(row) -> str:
    """Assemble the full classification prompt from base + label + source trust."""
    prompt = TOWER_ONLY_CLASSIFICATION_PROMPT if TOWER_ONLY else CLASSIFICATION_PROMPT
    label_hint = str(row.get("label", "")).strip().lower()
    if label_hint == "stealth":
        prompt += (
            "\n\nNOTE: This site is tagged STEALTH. Expect a disguised mast "
            "(monopalm, monopine, canister shroud) or a tower integrated into "
            "a building (steeple, clock tower, faux facade, corner tower "
            "block). Do not dismiss a too-tall palm/pine, a tall narrow "
            "shadow, or a tower block as merely a tree or architecture "
            "unless clearly non-telecom."
        )
    prompt += INPUT_CONFIDENCE_PROMPTS[normalize_input_confidence(
        row.get("input_confidence"))]
    return prompt


def maybe_recheck_equipment(provider: str, clients: dict, res: dict, views: list,
                            input_confidence: str) -> dict:
    """Second pass when a trusted source expects gear but the model said false."""
    if not source_expects_asset(res, input_confidence=input_confidence):
        return res
    if res.get("cell_equipment") is True:
        return res
    site = str(res.get("site_type") or "").strip().lower()
    # Claimed-site empties and false cell calls get a second look.
    if res.get("cell_equipment") is not False and site not in {"other", "unclear"}:
        return res
    if len(views) < 2:
        return res
    recheck = classify_site(
        provider, clients, views, prompt=EQUIPMENT_RECHECK_PROMPT)
    if recheck.get("cell_equipment") is True:
        res["cell_equipment"] = True
        res["cell_equipment_confidence"] = recheck.get(
            "cell_equipment_confidence", res.get("cell_equipment_confidence"))
        res["cell_equipment_evidence"] = recheck.get(
            "cell_equipment_evidence", res.get("cell_equipment_evidence"))
        if recheck.get("site_type") in _positive_site_types():
            res["site_type"] = recheck["site_type"]
            res["site_confidence"] = recheck.get(
                "site_confidence", res.get("site_confidence"))
            res["site_evidence"] = recheck.get(
                "site_evidence", res.get("site_evidence"))
        normalize_model_result(res)
        _step_done("equipment recheck", _brief_pass_result(res))
    else:
        _step_done("equipment recheck", "still no cell gear")
    return res


_PROMOTE_TO_STEALTH_SUBTYPES = frozenset(
    {"flagpole", "other_tower", "monopole", "unclear"}
)


def gate_weak_stealth_tower_claim(res: dict) -> dict:
    """Demote speculative stealth tower calls (common Gemini false positive).

    Stealth requires a purpose-built disguise (monopalm/monopine/canister).
    Architecture "likely conceals" guesses are demoted. A flagpole/monopole
    described as a palm/pine/canister mast is promoted to stealth so it is
    not uploaded as Flagpole.
    """
    if str(res.get("site_type") or "").strip().lower() != "tower":
        return res

    evidence = evidence_text(res).lower()
    strong_form = has_stealth_form(evidence)
    subtype = str(res.get("tower_subtype") or "").strip().lower()
    if subtype in _PROMOTE_TO_STEALTH_SUBTYPES and strong_form:
        res["tower_subtype"] = "stealth"
        subtype = "stealth"
        prior = str(res.get("site_evidence") or "").strip()
        note = "stealth gate: disguised mast (palm/pine/canister)"
        res["site_evidence"] = f"{prior} | {note}".strip(" |")
        _step_done("stealth gate", note)

    if subtype != "stealth":
        return res

    speculative = has_any(evidence, SPECULATIVE_STEALTH_CUES)
    has_cues = has_telecom_cue(evidence) or has_stealth_hardware(
        evidence
    )

    # Keep a described monopalm/monopine/canister. Speculative architecture
    # without those forms still gets demoted.
    if strong_form and not speculative:
        return res
    if strong_form and has_cues:
        return res

    prior = str(res.get("site_evidence") or "").strip()
    res["tower_subtype"] = "other_tower"
    note = "stealth gate: speculative or weak disguise cues"
    if speculative or not strong_form:
        # Do not keep cell=true on architecture-as-stealth guesses.
        if res.get("cell_equipment") is True and (speculative or not has_cues):
            res["cell_equipment"] = None
            res["cell_gear_kind"] = "unclear"
            res["cell_equipment_evidence"] = (
                f"{res.get('cell_equipment_evidence') or ''} | {note}"
            ).strip(" |")
    res["site_evidence"] = f"{prior} | {note}".strip(" |")
    normalize_model_result(res)
    _step_done("stealth gate", note)
    return res


def gate_weak_rooftop_cell_claim(res: dict) -> dict:
    """Downgrade untrustworthy rooftop cell=true claims before crop/dual-model.

    Requires telecom evidence cues and a compact Nearmap localization box.
    Oblique boxes are preferred; Vert-only true needs high conf + cues.
    Clears misleading HVAC boxes when the claim is demoted.
    """
    if str(res.get("site_type") or "").strip().lower() != "rooftop":
        return res
    if res.get("cell_equipment") is not True:
        return res

    evidence = evidence_text(res)
    has_cues = has_telecom_cue(evidence)
    oblique_box = has_locked_oblique_asset_box(res)
    valid = get_valid_asset_box(res)
    view = str(res.get("asset_view") or "").strip().lower()
    vert_box = bool(valid) and (
        "top-down" in view or ("vert" in view and "oblique" not in view)
    )
    conf = normalize_confidence(res.get("cell_equipment_confidence")) or 0.0

    if oblique_box and has_cues:
        return res
    if vert_box and has_cues and conf >= 0.85:
        return res

    prior = str(res.get("cell_equipment_evidence") or "").strip()
    reason_bits = []
    if not has_cues:
        reason_bits.append("no telecom cues")
    if not oblique_box and not vert_box:
        reason_bits.append("no usable Nearmap box")
    elif vert_box and conf < 0.85:
        reason_bits.append("Vert box needs conf>=0.85 or oblique")
    note = "first-pass gate: " + (", ".join(reason_bits) or "weak cell claim")
    res["cell_equipment"] = None
    res["cell_equipment_evidence"] = f"{prior} | {note}".strip(" |")
    res["cell_gear_kind"] = "unclear"
    res["asset_box_2d"] = None
    res["asset_view"] = None
    res["dual_model_resolution"] = res.get("dual_model_resolution") or "first_pass_gate"
    normalize_model_result(res)
    _step_done("first-pass cell gate", note)
    return res


def rooftop_box_is_usable(res: dict, views: list | None = None) -> bool:
    """True when asset_box_2d is geometrically valid and matches a real view."""
    if not get_valid_asset_box(res):
        return False
    view = str(res.get("asset_view") or "").strip()
    if not view:
        return False
    if views is None:
        return True
    picked = pick_view_for_asset_box(res, views)
    if picked is None:
        return False
    label, _img = picked
    if asset_view_is_nearmap_oblique(view) and "naip" in str(label).lower():
        return False
    return True


def maybe_repair_rooftop_asset_box(
    provider: str, clients: dict, res: dict, views: list
) -> dict:
    """Re-ask for a tight oblique box when rooftop localization is incomplete.

    Runs when site_type=rooftop and either cell=true or a strong rooftop call
    lacks a usable box. Without a box, crop + dual-model cannot be trustworthy.
    """
    if str(res.get("site_type") or "").strip().lower() != "rooftop":
        return res
    if rooftop_box_is_usable(res, views):
        # Persist coerced geometry.
        valid = get_valid_asset_box(res)
        if valid:
            res["asset_box_2d"] = valid
        return res

    conf = normalize_confidence(res.get("site_confidence")) or 0.0
    needs_repair = res.get("cell_equipment") is True or conf >= 0.7
    if not needs_repair:
        _step_done("box repair", "skipped (weak rooftop, no cell claim)")
        return res

    repair_views = _oblique_views_only(views)
    if not repair_views:
        _step_done("box repair", "skipped (no Nearmap obliques)")
        return res

    repair = classify_site(
        provider, clients, repair_views, prompt=ROOFTOP_BOX_REPAIR_PROMPT
    )
    normalize_model_result(repair)

    # Always absorb a usable repaired box.
    if coerce_asset_box(repair.get("asset_box_2d")) and repair.get("asset_view"):
        trial = dict(res)
        trial["asset_box_2d"] = repair.get("asset_box_2d")
        trial["asset_view"] = repair.get("asset_view")
        if rooftop_box_is_usable(trial, views) or rooftop_box_is_usable(
            trial, repair_views
        ):
            res["asset_box_2d"] = coerce_asset_box(repair.get("asset_box_2d"))
            res["asset_view"] = repair.get("asset_view")
            if repair.get("cell_equipment") is True:
                res["cell_equipment"] = True
                if repair.get("cell_equipment_confidence") is not None:
                    res["cell_equipment_confidence"] = repair.get(
                        "cell_equipment_confidence"
                    )
                if repair.get("cell_equipment_evidence"):
                    res["cell_equipment_evidence"] = repair.get(
                        "cell_equipment_evidence"
                    )
                if repair.get("cell_gear_kind"):
                    res["cell_gear_kind"] = repair.get("cell_gear_kind")
            elif repair.get("cell_equipment") is False:
                res["cell_equipment"] = False
                if repair.get("cell_equipment_evidence"):
                    res["cell_equipment_evidence"] = repair.get(
                        "cell_equipment_evidence"
                    )
                res["cell_gear_kind"] = repair.get("cell_gear_kind") or "none"
            normalize_model_result(res)
            _step_done(
                "box repair",
                f"repaired on {res.get('asset_view')} cell={res.get('cell_equipment')!r}",
            )
            return res

    if repair.get("cell_equipment") is False:
        res["cell_equipment"] = False
        if repair.get("cell_equipment_evidence"):
            res["cell_equipment_evidence"] = repair.get("cell_equipment_evidence")
        res["cell_gear_kind"] = "none"
        res["asset_box_2d"] = None
        res["asset_view"] = None
        normalize_model_result(res)
        _step_done("box repair", "no cellular gear — cell=false")
        return res

    _step_done("box repair", "failed (still no usable box)")
    return res


def enforce_rooftop_cell_requires_box(res: dict, views: list | None = None) -> dict:
    """Rooftop cell=true is incomplete without a usable localization box."""
    if str(res.get("site_type") or "").strip().lower() != "rooftop":
        return res
    if res.get("cell_equipment") is not True:
        return res
    if rooftop_box_is_usable(res, views):
        valid = get_valid_asset_box(res)
        if valid:
            res["asset_box_2d"] = valid
        return res
    res["cell_equipment"] = None
    res["cell_models_agree"] = False
    res["dual_model_resolution"] = "box_required"
    prior = str(res.get("cell_equipment_evidence") or "").strip()
    note = "Cell claim cleared: no usable asset_box_2d on a matching view."
    res["cell_equipment_evidence"] = f"{prior} | {note}".strip(" |")
    normalize_model_result(res)
    _step_done("box gate", "cell cleared (box required)")
    return res


def confirm_rooftop_cell_with_claude(
    res: dict,
    clients: dict,
    views: list,
    *,
    already_escalated: bool,
    allow_soft_keep: bool = True,
    from_wide_rescue: bool = False,
    used_crop: bool = False,
    allow_gemini_solo: bool = True,
    all_views: list | None = None,
) -> tuple[dict, str | None, bool]:
    """Tiered Claude check for rooftop/tower cell=true.

    1) Strong Gemini (optional) → skip Claude when allow_gemini_solo.
    2) usable Gemini crop → Claude votes true/false on THAT box only.
       A crop no is not final for rooftops: fall back to full-scene localize
       so a too-tight crop cannot unqualify a real array. Clear HVAC after
       localize still vetoes.
    3) No crop → Claude localizes; cell=true requires Claude box + IoU.

    Enrichment auto-apply should pass allow_soft_keep=False and
    allow_gemini_solo=False so rooftop HVAC FPs still need Claude hard-agree.
    Towers with Gemini site_confidence >= GEMINI_SOLO_CELL_CONF skip Claude
    even when allow_gemini_solo is False (Gemini already locked the tower).
    """
    site = str(res.get("site_type") or "").strip().lower()
    if site not in {"rooftop", "tower"}:
        return res, None, False

    def _apply_soft_keep(reason: str) -> tuple[dict, str | None, bool]:
        res["cell_equipment"] = True
        pre_conf = res.get("gemini_pre_escalation_cell_conf")
        pre_ev = res.get("gemini_pre_escalation_evidence")
        pre_gear = res.get("gemini_pre_escalation_gear")
        if pre_conf is not None:
            res["cell_equipment_confidence"] = pre_conf
        elif res.get("cell_equipment_confidence") is None:
            pass
        if pre_ev:
            res["cell_equipment_evidence"] = (
                f"{pre_ev} | Claude veto soft-kept (Nearmap+Gemini)"
            )
        if pre_gear:
            res["cell_gear_kind"] = pre_gear
        res["gemini_cell_equipment"] = True
        res["cell_models_agree"] = True
        res["dual_model_resolution"] = "soft_keep_gemini"
        normalize_model_result(res)
        _step_done("dual-model cell", reason)
        return res, "claude", True

    def _soft_keep_candidate() -> dict:
        trial = dict(res)
        trial["cell_equipment"] = True
        if res.get("gemini_pre_escalation_cell_conf") is not None:
            trial["cell_equipment_confidence"] = res.get(
                "gemini_pre_escalation_cell_conf"
            )
        if res.get("gemini_pre_escalation_evidence"):
            trial["cell_equipment_evidence"] = res.get(
                "gemini_pre_escalation_evidence"
            )
        return trial

    def _apply_claude_veto(claude_res: dict, *, reason: str) -> tuple[dict, str | None, bool]:
        # Crop veto: never soft-keep the same boxed region Claude rejected.
        can_claimed = (
            not used_crop
            and should_keep_claimed_gemini_tower(
                res, from_wide_rescue=from_wide_rescue
            )
        )
        if can_claimed:
            if gemini_evidence or claude_res.get("cell_equipment_evidence"):
                res["cell_equipment_evidence"] = (
                    f"{gemini_evidence or ''} | claimed-site keep Gemini "
                    f"(Claude missed disguised mast); Claude: "
                    f"{claude_res.get('cell_equipment_evidence') or 'cell=false'}"
                ).strip(" |")
            res["cell_equipment"] = True
            pre_conf = res.get("gemini_pre_escalation_cell_conf")
            pre_gear = res.get("gemini_pre_escalation_gear")
            if pre_conf is not None:
                res["cell_equipment_confidence"] = pre_conf
            if pre_gear:
                res["cell_gear_kind"] = pre_gear
            res["gemini_cell_equipment"] = True
            res["claude_cell_equipment"] = claude_res.get("cell_equipment")
            res["cell_models_agree"] = False
            res["dual_model_resolution"] = "claimed_site_keep_gemini"
            normalize_model_result(res)
            _step_done(
                "dual-model cell",
                "claimed-site keep Gemini (stealth/canister mast)",
            )
            return res, "claude", False
        can_soft = (
            allow_soft_keep
            and not used_crop
            and should_soft_keep_gemini_cell(
                res, from_wide_rescue=from_wide_rescue
            )
        )
        if can_soft:
            if gemini_evidence or claude_res.get("cell_equipment_evidence"):
                res["cell_equipment_evidence"] = (
                    f"{gemini_evidence or ''} | Claude veto soft-kept "
                    f"(Nearmap+Gemini); Claude: "
                    f"{claude_res.get('cell_equipment_evidence') or 'cell=false'}"
                ).strip(" |")
            return _apply_soft_keep(
                "disagree → soft-keep Gemini (Nearmap+Gemini)"
            )
        res["cell_equipment"] = claude_res.get("cell_equipment")
        if claude_res.get("cell_equipment_evidence"):
            res["cell_equipment_evidence"] = claude_res.get(
                "cell_equipment_evidence"
            )
        if claude_res.get("cell_gear_kind"):
            res["cell_gear_kind"] = claude_res.get("cell_gear_kind")
        if "cell_equipment_confidence" in claude_res:
            res["cell_equipment_confidence"] = claude_res.get(
                "cell_equipment_confidence"
            )
        if not has_locked_oblique_asset_box(res):
            res["asset_box_2d"] = None
            res["asset_view"] = None
        res["dual_model_resolution"] = "claude_veto"
        res["cell_models_agree"] = False
        normalize_model_result(res)
        _step_done("dual-model cell", reason)
        return res, "claude", False

    if res.get("cell_equipment") is not True:
        res["claude_cell_equipment"] = res.get("cell_equipment")
        res["gemini_cell_equipment"] = res.get("gemini_pre_escalation_cell")
        if (
            already_escalated
            and allow_soft_keep
            and res.get("gemini_pre_escalation_cell") is True
            and should_soft_keep_gemini_cell(
                _soft_keep_candidate(), from_wide_rescue=from_wide_rescue
            )
        ):
            return _apply_soft_keep(
                "disagree → soft-keep Gemini (Nearmap+Gemini)"
            )
        res["cell_models_agree"] = False
        return res, ("claude" if already_escalated else None), False

    if site == "rooftop" and not _has_nearmap_context(res):
        res["claude_cell_equipment"] = None
        res["cell_models_agree"] = False
        _step_done("dual-model cell", "skipped (NAIP-only rooftop)")
        return res, None, False

    res["gemini_cell_equipment"] = True
    gemini_conf = res.get("cell_equipment_confidence")
    gemini_evidence = res.get("cell_equipment_evidence")
    gemini_gear = res.get("cell_gear_kind")
    gemini_box = get_valid_asset_box(res)
    gemini_view = str(res.get("asset_view") or "").strip()
    # Preserve Gemini snapshot for soft-keep if not already stamped.
    res.setdefault("gemini_pre_escalation_cell", True)
    res.setdefault("gemini_pre_escalation_cell_conf", gemini_conf)
    res.setdefault("gemini_pre_escalation_evidence", gemini_evidence)
    res.setdefault("gemini_pre_escalation_gear", gemini_gear)

    if already_escalated:
        agree = res.get("cell_equipment") is True
        res["claude_cell_equipment"] = res.get("cell_equipment")
        res["cell_models_agree"] = agree
        if agree:
            res["dual_model_resolution"] = res.get("dual_model_resolution") or "agree"
        _step_done(
            "dual-model cell",
            "agree" if agree else "disagree (Claude negative)",
        )
        return res, "claude", agree

    if should_skip_claude_for_gemini_tower(
        res, from_wide_rescue=from_wide_rescue
    ):
        res["claude_cell_equipment"] = None
        res["cell_models_agree"] = True
        res["dual_model_resolution"] = "gemini_strong_solo"
        normalize_model_result(res)
        _step_done(
            "dual-model cell",
            f"skipped Claude (Gemini tower conf>={GEMINI_SOLO_CELL_CONF:g})",
        )
        return res, "gemini_strong_solo", True

    site_conf = normalize_confidence(res.get("site_confidence"))
    if site_conf is None or site_conf < CLAUDE_ESCALATE_MIN_SITE_CONF:
        res["claude_cell_equipment"] = None
        res["cell_models_agree"] = False
        _step_done(
            "dual-model cell",
            f"skipped Claude (Gemini site conf<{CLAUDE_ESCALATE_MIN_SITE_CONF:g})",
        )
        return res, None, False

    if allow_gemini_solo and should_trust_gemini_cell_solo(
        res, from_wide_rescue=from_wide_rescue
    ):
        res["claude_cell_equipment"] = None
        res["cell_models_agree"] = True
        res["dual_model_resolution"] = "gemini_strong_solo"
        normalize_model_result(res)
        _step_done(
            "dual-model cell",
            f"skipped Claude (Gemini solo conf>={GEMINI_SOLO_CELL_CONF:g})",
        )
        return res, "gemini_strong_solo", True

    if "claude" not in clients or clients.get("claude") is None:
        res["cell_models_agree"] = False
        _step_done("dual-model cell", "skipped (no Claude client)")
        return res, None, False

    if used_crop:
        prompt = cell_confirm_prompt(site, used_crop=True)
        mode = "crop_check"
    else:
        prompt = cell_confirm_prompt(site, used_crop=False)
        mode = "localize_iou"
    if site == "tower" and source_expects_asset(res):
        prompt += CLAIMED_SITE_TOWER_CONFIRM_NOTE

    claude_res = classify_site(
        "claude",
        clients,
        views,
        prompt=prompt,
        claude_model=CLAUDE_CROP_MODEL if used_crop else CLAUDE_ESCALATION_MODEL,
    )
    claude_cell = claude_res.get("cell_equipment") is True
    res["claude_cell_equipment"] = claude_res.get("cell_equipment")
    res["claude_dual_mode"] = mode

    if not claude_cell:
        loc_views = all_views or []
        if used_crop and site == "rooftop" and loc_views:
            loc_res = classify_site(
                "claude",
                clients,
                loc_views,
                prompt=ROOFTOP_CELL_LOCALIZE_CONFIRM_PROMPT,
                claude_model=CLAUDE_ESCALATION_MODEL,
            )
            loc_box = get_valid_asset_box(loc_res)
            if loc_res.get("cell_equipment") is True and loc_box:
                res["claude_cell_equipment"] = True
                res["claude_dual_mode"] = "crop_then_localize"
                if loc_res.get("cell_equipment_confidence") is not None:
                    res["cell_equipment_confidence"] = loc_res.get(
                        "cell_equipment_confidence"
                    )
                if loc_res.get("cell_equipment_evidence"):
                    res["cell_equipment_evidence"] = (
                        f"{gemini_evidence or ''} | crop inconclusive; "
                        f"localize: {loc_res.get('cell_equipment_evidence')}"
                    ).strip(" |")
                if loc_res.get("cell_gear_kind"):
                    res["cell_gear_kind"] = loc_res.get("cell_gear_kind")
                res["asset_box_2d"] = loc_box
                res["asset_view"] = loc_res.get("asset_view") or res.get("asset_view")
                res["cell_equipment"] = True
                res["cell_models_agree"] = True
                res["dual_model_resolution"] = "agree_localize"
                normalize_model_result(res)
                _step_done(
                    "dual-model cell",
                    "crop no → localize agree (tight crop fallback)",
                )
                return res, "claude", True
            claude_res = loc_res
            res["claude_cell_equipment"] = loc_res.get("cell_equipment")
        return _apply_claude_veto(
            claude_res,
            reason=(
                "crop+localize veto"
                if used_crop and site == "rooftop"
                else "crop veto (HVAC/not cell)"
                if used_crop
                else "localize veto"
            ),
        )

    # Claude says true.
    if used_crop:
        # Keep Gemini's box/coords; Claude only confirmed the crop contents.
        if claude_res.get("cell_equipment_confidence") is not None:
            res["cell_equipment_confidence"] = claude_res.get(
                "cell_equipment_confidence"
            )
        if claude_res.get("cell_equipment_evidence"):
            res["cell_equipment_evidence"] = claude_res.get(
                "cell_equipment_evidence"
            )
        if claude_res.get("cell_gear_kind"):
            res["cell_gear_kind"] = claude_res.get("cell_gear_kind")
        res["cell_equipment"] = True
        res["cell_models_agree"] = True
        res["dual_model_resolution"] = "agree_crop"
        normalize_model_result(res)
        _step_done("dual-model cell", "agree (Claude crop check)")
        return res, "claude", True

    # Localize path: Claude must produce a usable box.
    claude_box = get_valid_asset_box(claude_res)
    claude_view = str(claude_res.get("asset_view") or "").strip()
    if not claude_box:
        claude_res = dict(claude_res)
        claude_res["cell_equipment"] = False
        claude_res["cell_equipment_evidence"] = (
            str(claude_res.get("cell_equipment_evidence") or "")
            + " | localize: Claude true without usable box"
        ).strip(" |")
        return _apply_claude_veto(
            claude_res, reason="localize true without box"
        )

    if gemini_box and gemini_view and claude_view and site != "tower":
        same_view = (
            gemini_view.lower() in claude_view.lower()
            or claude_view.lower() in gemini_view.lower()
        )
        if same_view:
            iou = box_iou(gemini_box, claude_box)
            res["claude_gemini_box_iou"] = round(iou, 3)
            if iou < GEMINI_CLAUDE_BOX_IOU:
                claude_res = dict(claude_res)
                claude_res["cell_equipment"] = False
                claude_res["cell_equipment_evidence"] = (
                    f"{claude_res.get('cell_equipment_evidence') or ''} | "
                    f"box IoU {iou:.2f} < {GEMINI_CLAUDE_BOX_IOU:g} vs Gemini"
                ).strip(" |")
                return _apply_claude_veto(
                    claude_res, reason=f"box IoU {iou:.2f} mismatch"
                )

    # Towers: Gemini often boxes the full mast/compound; Claude boxes the
    # antenna array. Prefer Claude's tighter box. Rooftops keep Gemini when
    # IoU already aligned above; otherwise adopt Claude localization.
    if site == "tower" or not gemini_box:
        res["asset_box_2d"] = claude_box
        res["asset_view"] = claude_view or res.get("asset_view")
    if claude_res.get("cell_equipment_confidence") is not None:
        res["cell_equipment_confidence"] = claude_res.get(
            "cell_equipment_confidence"
        )
    if claude_res.get("cell_equipment_evidence"):
        res["cell_equipment_evidence"] = claude_res.get("cell_equipment_evidence")
    if claude_res.get("cell_gear_kind"):
        res["cell_gear_kind"] = claude_res.get("cell_gear_kind")
    res["cell_equipment"] = True
    res["cell_models_agree"] = True
    res["dual_model_resolution"] = "agree_localize"
    normalize_model_result(res)
    _step_done("dual-model cell", "agree (Claude localize)")
    return res, "claude", True


def nearmap_full_blocks_rescue(
    res: dict, *, nearmap_tier: str, has_obliques: bool
) -> bool:
    """True when full Nearmap+obliques locked onto a specific HVAC-only rooftop.

    Only blocks wide/zoom when we have a compact Nearmap oblique asset box —
    that means we are on the right building and should not invent cell gear
    from a wider NAIP scout.

    Without a locked box (common when the SF pin sits in a parking lot or
    empty pavement next to a mall), wide/zoom + pin re-center must still run
    so a nearby rooftop/tower can be recovered.
    """
    if str(nearmap_tier or "").strip().lower() != "full" or not has_obliques:
        return False
    site = str(res.get("site_type") or "").strip().lower()
    if site != "rooftop" or res.get("cell_equipment") is not False:
        return False
    return has_locked_oblique_asset_box(res)


def has_locked_oblique_asset_box(res: dict) -> bool:
    """True when a compact Nearmap oblique box names a specific roof/host."""
    if not asset_view_is_nearmap_oblique(res.get("asset_view")):
        return False
    return get_valid_asset_box(res) is not None


def needs_pin_offset_scout(res: dict) -> bool:
    """True when a rooftop pin may have missed facade/parapet gear.

    Rooftops buy a Nearmap wide AOI on the host building. Other/unclear
    without Nearmap coverage use cheap NAIP wide/zoom instead.
    """
    if str(res.get("site_type") or "").strip().lower() != "rooftop":
        return False
    return res.get("cell_equipment") is not True


def scout_result_wins(prior: dict, scout: dict) -> bool:
    """True when a wide/zoom/address scout should replace the pin result.

    A rooftop/tower label is not enough: confidence must clear TIER_CONF_MEDIUM
    and must not drop versus an already-positive prior.
    """
    scout_site = str(scout.get("site_type") or "").strip().lower()
    prior_site = str(prior.get("site_type") or "").strip().lower()
    scout_conf = normalize_confidence(scout.get("site_confidence")) or 0.0
    prior_conf = normalize_confidence(prior.get("site_confidence")) or 0.0
    if scout_site in _positive_site_types():
        if scout_conf < TIER_CONF_MEDIUM:
            return False
        if prior_site in _positive_site_types() and scout_conf < prior_conf:
            return False
        return True
    return scout_conf > prior_conf


def needs_naip_rescue(res: dict) -> bool:
    """True when a weak NAIP other/unclear still warrants NAIP wide/zoom.

    Very low-confidence ``unclear`` (below TIER_CONF_MEDIUM) is not a signal —
    do not spend wide/zoom hunting an empty chip.
    """
    site = str(res.get("site_type") or "").strip().lower()
    conf = normalize_confidence(res.get("site_confidence"))
    if site == "unclear":
        return conf is not None and conf >= TIER_CONF_MEDIUM
    if site == "other":
        return not confident_no_asset(res)
    return False


def confident_no_asset(res: dict) -> bool:
    """True when Gemini already locked a high-confidence empty/other chip.

    Outreach-verified (high source trust) only locks empty at
    EMPTY_CHIP_LOCK_CONF (0.90). Weaker other calls still get zoom/Claude.
    """
    if str(res.get("site_type") or "").strip().lower() != "other":
        return False
    conf = normalize_confidence(res.get("site_confidence"))
    if conf is None:
        return False
    lock = (
        EMPTY_CHIP_LOCK_CONF
        if source_expects_asset(res)
        else TIER_CONF_HIGH
    )
    return conf >= lock


def stamp_naip_screen(res: dict) -> dict:
    """Keep the NAIP-screen label after Nearmap overwrites site_type."""
    if res.get("naip_screen_site_type") not in (None, ""):
        return res
    res["naip_screen_site_type"] = res.get("site_type")
    res["naip_screen_site_confidence"] = res.get("site_confidence")
    res["naip_screen_cell_equipment"] = res.get("cell_equipment")
    return res


def needs_flash_confirm(res: dict) -> bool:
    """Promote the NAIP lite screen to Flash for anything that is not a confident other.

    Low-confidence ``unclear`` stays on the screen result — Flash will not invent
    a tower from a 0.1 chip.
    """
    site = str(res.get("site_type") or "").strip().lower()
    if site in _positive_site_types():
        return True
    if site == "unclear":
        conf = normalize_confidence(res.get("site_confidence"))
        return conf is not None and conf >= TIER_CONF_MEDIUM
    if site == "other":
        return not confident_no_asset(res)
    return False


def should_keep_claimed_gemini_tower(res: dict, *, from_wide_rescue: bool = False) -> bool:
    """True when outreach-verified Gemini found a disguised mast Claude missed.

    Rooftop HVAC vetoes still stand. Only keep a ground tower whose evidence
    already names a monopalm/monopine/canister (or equivalent stealth form).
    """
    if from_wide_rescue:
        return False
    if not source_expects_asset(res):
        return False
    if str(res.get("site_type") or "").strip().lower() != "tower":
        return False
    if (
        res.get("cell_equipment") is not True
        and res.get("gemini_pre_escalation_cell") is not True
    ):
        return False
    evidence = " ".join(
        str(res.get(key) or "")
        for key in (
            "gemini_pre_escalation_evidence",
            "cell_equipment_evidence",
            "site_evidence",
        )
    )
    return has_stealth_form(evidence) or has_stealth_hardware(evidence)


def should_soft_keep_gemini_cell(res: dict, *, from_wide_rescue: bool) -> bool:
    """Allow Gemini cell=true to win a Claude veto when Nearmap+Gemini are strong.

    Requires full Nearmap obliques, high cell conf, telecom cues, and a compact
    asset box drawn on a Nearmap oblique (not NAIP / whole-roof boxes).
    Never soft-keep after an unvalidated wide-AOI resurrection.
    """
    if from_wide_rescue:
        return False
    if str(res.get("nearmap_tier") or "").strip().lower() != "full":
        return False
    views = str(res.get("nearmap_views") or "").lower()
    if not any(d in views for d in ("north", "east", "south", "west")):
        return False
    if not asset_view_is_nearmap_oblique(res.get("asset_view")):
        return False
    valid = get_valid_asset_box(res)
    if not valid:
        return False
    ymin, xmin, ymax, xmax = valid
    # Soft-keep requires a compact gear box (not a half-roof blob).
    if (ymax - ymin) > 400 or (xmax - xmin) > 400:
        return False
    conf = normalize_confidence(res.get("cell_equipment_confidence"))
    if conf is None or conf < GEMINI_SOFT_KEEP_CELL_CONF:
        return False
    evidence = evidence_text(res)
    return has_telecom_cue(evidence)


def should_skip_claude_for_gemini_tower(
    res: dict, *, from_wide_rescue: bool = False
) -> bool:
    """Skip Claude when Gemini already locked a tower at >= GEMINI_SOLO_CELL_CONF.

    Wide-AOI / zoom rescues still go through Claude (wrong-neighbor risk).
    Rooftops are not skipped here — HVAC FPs need the dual-model crop vote.
    """
    if from_wide_rescue:
        return False
    if str(res.get("site_type") or "").strip().lower() != "tower":
        return False
    if res.get("cell_equipment") is not True:
        return False
    conf = normalize_confidence(res.get("site_confidence"))
    return conf is not None and conf >= GEMINI_SOLO_CELL_CONF


def gemini_confidence_locks_claude(res: dict) -> bool:
    """True when Gemini site_confidence is high enough to skip full-scene Claude.

    ``unclear`` never locks. Rooftop cell-unconfirmed is checked first in
    ``escalation_reason`` and still escalates. Towers lock at
    GEMINI_SOLO_CELL_CONF; empty/other and rooftops stay at EMPTY_CHIP_LOCK_CONF
    so HVAC FPs still get Claude.
    """
    site = str(res.get("site_type") or "").strip().lower()
    if site == "unclear":
        return False
    conf = normalize_confidence(res.get("site_confidence"))
    if conf is None:
        return False
    if site in {"other", "rooftop"}:
        return conf >= EMPTY_CHIP_LOCK_CONF
    return conf >= GEMINI_SOLO_CELL_CONF


def should_trust_gemini_cell_solo(res: dict, *, from_wide_rescue: bool = False) -> bool:
    """Skip Claude dual-confirm when Gemini already locked a strong rooftop cell.

    Requires soft-keep-strength Nearmap localization plus conf >= EMPTY_CHIP_LOCK_CONF.
    Saves Claude cost and avoids Claude false vetoes on clear antenna sites.
    Rooftop HVAC FPs still need Claude below that lock.
    """
    if res.get("cell_equipment") is not True:
        return False
    if not should_soft_keep_gemini_cell(res, from_wide_rescue=from_wide_rescue):
        return False
    conf = normalize_confidence(res.get("cell_equipment_confidence"))
    if conf is None or conf < EMPTY_CHIP_LOCK_CONF:
        return False
    return has_locked_oblique_asset_box(res)


def align_site_evidence_with_cell(res: dict) -> dict:
    """Keep site_evidence from claiming cellular gear when cell call is not true."""
    if res.get("cell_equipment") is True:
        return res
    site_ev = str(res.get("site_evidence") or "")
    if not site_ev:
        return res
    if has_telecom_cue(site_ev):
        res["site_evidence"] = (
            "Host structure visible on imagery; cellular gear was not confirmed "
            f"(final cell={res.get('cell_equipment')!r})."
        )
        _step_done("site evidence align", "cleared cellular claims (cell not true)")
    return res


def build_cell_confirm_views(res: dict, views: list) -> tuple[list, bool]:
    """Crop the asset box for dual-model confirm when possible.

    Towers skip crop: Gemini's mast/compound box often misses the antenna
    array (or lands on foliage / a tank rim), and the rooftop HVAC crop
    prompt then false-vetoes a tower that is obvious in the full Nearmap
    stack. Rooftops still crop so Claude votes on the boxed HVAC-vs-panel
    region.

    Returns (views_for_confirm, used_crop).
    """
    if str(res.get("site_type") or "").strip().lower() == "tower":
        return views, False
    valid = get_valid_asset_box(res)
    picked = pick_view_for_asset_box(res, views)
    if not valid or picked is None:
        return views, False
    label, img = picked
    # Rooftop cell confirm should not run on NAIP when an oblique box was claimed.
    if asset_view_is_nearmap_oblique(res.get("asset_view")) and (
        "naip" in str(label).lower()
    ):
        return views, False
    res["asset_box_2d"] = valid
    crop = _crop_zoom(img, valid, pad_frac=CELL_CONFIRM_PAD_FRAC)
    return [(f"cell crop ({label})", crop), (label, img)], True


def _is_gemini_3(model: str | None) -> bool:
    return "gemini-3" in str(model or "").lower()


def _is_screen_model(model: str | None) -> bool:
    use = str(model or "").strip()
    screen = str(GEMINI_SCREEN_MODEL or "").strip()
    return "lite" in use.lower() or (bool(screen) and use == screen)


def _gemini_thinking_level(model: str | None = None) -> str:
    """Gemini 3.x thinking_level for this model (NAIP screen vs Nearmap/confirm)."""
    if _is_screen_model(model or GEMINI_MODEL):
        return _parse_thinking_level(
            os.environ.get("GEMINI_SCREEN_THINKING_LEVEL"),
            GEMINI_SCREEN_THINKING_LEVEL,
        )
    return _parse_thinking_level(
        os.environ.get("GEMINI_THINKING_LEVEL"), GEMINI_THINKING_LEVEL
    )


def _gemini_thinking_budget(model: str | None = None) -> int:
    """Resolved thinking token budget for a Gemini vision model.

    Default is 0 (thinking is billed). Set GEMINI_THINKING_BUDGET=1024 to restore
    Flash thinking. Lite models always stay at 0.
    """
    raw = GEMINI_THINKING_BUDGET_ENV or os.environ.get("GEMINI_THINKING_BUDGET", "").strip()
    use = str(model or GEMINI_MODEL or "").strip().lower()
    if "lite" in use:
        return 0
    if raw and raw.lower() not in {"auto", "default"}:
        try:
            return max(0, int(float(raw)))
        except (TypeError, ValueError):
            return 0
    return 0


def _gemini_generate_config(
    schema: dict, model: str | None = None
) -> genai_types.GenerateContentConfig:
    """Structured JSON config. Gemini 3.x uses thinking_level, not thinking_budget."""
    use_model = model or GEMINI_MODEL
    budget = _gemini_thinking_budget(use_model)
    name = str(use_model or "").lower()
    kwargs: dict = {
        "response_mime_type": "application/json",
        "response_schema": schema,
        "max_output_tokens": 8192 if budget > 0 else 1000,
    }
    if name.startswith("gemini-2.0"):
        pass
    elif _is_gemini_3(use_model):
        # thinking_budget on 3.x lite/preview returns 400 INVALID_ARGUMENT.
        level = _gemini_thinking_level(use_model)
        kwargs["thinking_config"] = genai_types.ThinkingConfig(thinking_level=level)
        kwargs["max_output_tokens"] = 1000 if level == "MINIMAL" else 8192
    else:
        kwargs["thinking_config"] = genai_types.ThinkingConfig(
            thinking_budget=budget
        )
    return genai_types.GenerateContentConfig(**kwargs)


def _gemini_fallback_settings() -> tuple[str, int, int, float]:
    """(GEMINI_FALLBACK_MODEL, _RETRIES, _AFTER, _COOLDOWN_S), read per call."""
    return (
        env_str("GEMINI_FALLBACK_MODEL", ""),
        max(0, int(env_float("GEMINI_FALLBACK_RETRIES", 3))),
        max(1, int(env_float("GEMINI_FALLBACK_AFTER", 2))),
        max(0.0, env_float("GEMINI_FALLBACK_COOLDOWN_S", 300)),
    )


def _gemini_call_model(client: genai.Client, contents: list, schema: dict,
                       model: str, retries: int | None) -> dict:
    """One model's call; the config is rebuilt for that model's family."""
    return llm.call_gemini_json(
        client,
        contents,
        model=model,
        config=_gemini_generate_config(schema, model=model),
        retries=retries,
    )


def _call_gemini_json(client: genai.Client, contents: list, schema: dict,
                      retries: int | None = None,
                      model: str | None = None) -> dict:
    """Gemini vision call with structured JSON output.

    With ``GEMINI_FALLBACK_MODEL`` set (and different from the primary), a
    primary call that still fails with 429/503 after ``GEMINI_RETRIES`` is
    re-sent to the fallback (``GEMINI_FALLBACK_RETRIES``); the reply carries
    ``model`` = fallback and ``model_fallback`` = True. After
    ``GEMINI_FALLBACK_AFTER`` consecutive final primary failures the
    process-wide breaker sends calls straight to the fallback for
    ``GEMINI_FALLBACK_COOLDOWN_S`` before one call probes the primary again.
    Other errors (and fallback failures) raise as before.
    """
    use_model = model or GEMINI_MODEL
    fallback, fb_retries, fb_after, fb_cooldown = _gemini_fallback_settings()
    if not fallback or fallback == use_model:
        res = _gemini_call_model(client, contents, schema, use_model, retries)
        normalize_model_result(res)
        res["model"] = use_model
        return res

    breaker = llm.GEMINI_FALLBACK_BREAKER
    route = breaker.route(use_model)
    if route == breaker.FALLBACK:
        res = _gemini_call_model(client, contents, schema, fallback, fb_retries)
    else:
        try:
            res = _gemini_call_model(client, contents, schema, use_model, retries)
        except Exception as exc:
            if not llm.is_gemini_overload(exc):
                if route == breaker.PROBE:
                    breaker.release_probe(use_model)
                raise
            opened = breaker.record_overload(
                use_model, after=fb_after, cooldown_s=fb_cooldown, fallback=fallback
            )
            if not opened:
                llm.logger.warning(
                    "Gemini %s failed after retries (%s) — retrying this call on "
                    "fallback %s", use_model, llm._gemini_http_status(exc), fallback,
                )
            res = _gemini_call_model(client, contents, schema, fallback, fb_retries)
        else:
            breaker.record_success(use_model)
            normalize_model_result(res)
            res["model"] = use_model
            return res
    normalize_model_result(res)
    res["model"] = fallback
    res["model_fallback"] = True
    res["model_requested"] = use_model
    return res


def _call_claude_json(client: Anthropic, content: list, schema: dict,
                      tool_name: str, retries: int = 3,
                      model: str | None = None) -> dict:
    """Claude vision call with tool-forced JSON; hops CLAUDE_MODELS when unpinned."""
    res, use_model = llm.call_claude_json(
        client, content, schema, tool_name, retries=retries, model=model
    )
    normalize_model_result(res)
    res["model"] = use_model
    return res


def classify_site(provider: str, clients: dict,
                  views: list[tuple[str, Image.Image]],
                  prompt: str = CLASSIFICATION_PROMPT, retries: int | None = None,
                  scan: bool = False, claude_model: str | None = None,
                  gemini_model: str | None = None,
                  trim_views: bool = True,
                  image_max_px: int | None = None) -> dict:
    """Classify one asset via Gemini or Claude using the same prompt.

    ``retries`` None: Gemini uses GEMINI_RETRIES (env), Claude its default 3.
    """
    send_views = (
        trim_views_for_model(views, max_px=image_max_px)
        if trim_views
        else list(views)
    )
    if scan:
        claude_schema, gemini_schema = SCAN_SCHEMA, GEMINI_SCAN_SCHEMA
        tool_name = "scan_candidates"
        classify_prompt = _active_scan_prompt()
    else:
        claude_schema, gemini_schema = _classification_response_schemas()
        tool_name = "classify_site"
        classify_prompt = prompt
    if provider == "gemini":
        contents = _views_to_gemini_contents(send_views, classify_prompt)
        return _call_gemini_json(
            clients["gemini"], contents, gemini_schema, retries,
            model=gemini_model or GEMINI_MODEL,
        )
    content = _views_to_claude_content(send_views, classify_prompt)
    return _call_claude_json(
        clients["claude"], content, claude_schema, tool_name,
        3 if retries is None else retries, model=claude_model)


def site_confidence_band(res: dict) -> str:
    """Map numeric site_confidence to high | medium | low for tier gating."""
    conf = normalize_confidence(res.get("site_confidence"))
    if conf is None:
        return "low"
    if conf >= TIER_CONF_HIGH:
        return "high"
    if conf >= TIER_CONF_MEDIUM:
        return "medium"
    return "low"


def _is_rooftop(res: dict) -> bool:
    return str(res.get("site_type") or "").strip().lower() == "rooftop"


def _rooftop_cell_confirmed(res: dict) -> bool:
    """True when rooftop cell gear is true at/above ROOFTOP_CELL_CONF_MIN.

    Missing cell_equipment_confidence still counts as confirmed when
    cell_equipment is True (model sometimes omits the numeric field).
    """
    if res.get("cell_equipment") is not True:
        return False
    cell_conf = normalize_confidence(res.get("cell_equipment_confidence"))
    if cell_conf is None:
        return True
    return cell_conf >= ROOFTOP_CELL_CONF_MIN


def tier_confident_stop(res: dict) -> bool:
    """True when tiered fetch can stop without pulling the next Nearmap tier.

    Towers: medium+ site conf and cell_equipment decided (true or false).
    Rooftops: medium+ site conf and confirmed cellular gear (true + conf bar).
    cell=false/null on a rooftop must continue — facade/parapet gear is often
    invisible on NAIP/Vert alone.
    """
    if res.get("site_type") not in _positive_site_types():
        return False
    if site_confidence_band(res) == "low":
        return False
    if _is_rooftop(res):
        return _rooftop_cell_confirmed(res)
    if res.get("cell_equipment") is None:
        return False
    return True


def db_backed_naip_tower_skip_nearmap_reason(
    res: dict, *, db_backed: bool = False
) -> str | None:
    """Skip Nearmap when FCC/TowerSource plus NAIP already decided a tower.

    Medium+ site confidence and cell_equipment true/false is enough — the
    database anchors the pin. Imagery-only towers still buy obliques.
    """
    if not db_backed:
        return None
    if str(res.get("site_type") or "").strip().lower() != "tower":
        return None
    if not tier_confident_stop(res):
        return None
    return "DB-hit NAIP tower medium+ cell decided"


def rooftop_naip_cell_skip_nearmap_reason(
    res: dict, naip_age_years: float | None = None
) -> str | None:
    """Skip Nearmap when NAIP already locked rooftop cell at the empty-chip lock."""
    if not _is_rooftop(res):
        return None
    if res.get("cell_equipment") is not True:
        return None
    cell_conf = normalize_confidence(res.get("cell_equipment_confidence"))
    if cell_conf is None or cell_conf < EMPTY_CHIP_LOCK_CONF:
        return None
    if site_confidence_band(res) == "low":
        return None
    if naip_age_blocks_early_stop(res, naip_age_years):
        return None
    return f"NAIP rooftop cell conf>={EMPTY_CHIP_LOCK_CONF:g}"


def naip_empty_osm_skip_nearmap_reason(
    res: dict,
    *,
    db_backed: bool = False,
    osm_info: dict | None = None,
    naip_age_years: float | None = None,
) -> str | None:
    """Skip Nearmap when NAIP is empty and OSM shows no building or tower.

    Fail-open: missing/failed OSM does not skip. DB-backed claimed towers
    still buy Nearmap if NAIP saw nothing. Stale NAIP (older than
    NAIP_MAX_AGE_YEARS) does not skip — OSM empty is not a substitute for
    current imagery.
    """
    if db_backed:
        return None
    if naip_age_years is not None:
        try:
            if float(naip_age_years) > float(NAIP_MAX_AGE_YEARS):
                return None
        except (TypeError, ValueError):
            pass
    site = str(res.get("site_type") or "").strip().lower()
    if site not in {"other", "unclear"}:
        return None
    try:
        from enrichment.osm_prefilter import osm_suggests_empty_chip
    except Exception:
        return None
    if not osm_suggests_empty_chip(osm_info):
        return None
    return "NAIP empty + OSM no building/tower"


def skip_nearmap_after_naip_reason(
    res: dict,
    *,
    db_backed: bool = False,
    osm_tower: bool = False,
    osm_info: dict | None = None,
    naip_age_years: float | None = None,
) -> str | None:
    """First skip-Nearmap reason after the NAIP pass, else None."""
    locked = locked_gemini_tower_skip_nearmap_reason(
        res, db_backed=db_backed, osm_tower=osm_tower
    )
    if locked:
        return locked
    medium = db_backed_naip_tower_skip_nearmap_reason(res, db_backed=db_backed)
    if medium:
        return medium
    roof = rooftop_naip_cell_skip_nearmap_reason(res, naip_age_years)
    if roof:
        return roof
    return naip_empty_osm_skip_nearmap_reason(
        res,
        db_backed=db_backed,
        osm_info=osm_info,
        naip_age_years=naip_age_years,
    )


def rooftop_requires_nearmap_tiers(res: dict) -> bool:
    """True when the remaining rooftop path should fetch Vert + obliques.

    Locked NAIP rooftop cell (>= EMPTY_CHIP_LOCK_CONF) skips earlier via
    rooftop_naip_cell_skip_nearmap_reason. Callers that reach this point
    still need paid imagery for facade confirmation.
    """
    return _is_rooftop(res)


def tower_cell_requires_nearmap_obliques(
    res: dict, *, db_backed: bool = False
) -> bool:
    """Whether a tower+cell call must continue to Nearmap obliques.

    Imagery-only (no FCC/TowerSource) always continues: pin-centered Vert
    often frames a neighbor rooftop or a different pole than the SF pin.

    DB-hit Gemini towers that already decided cell at medium+ site conf
    may stop on NAIP. Rooftops are handled separately by
    rooftop_requires_nearmap_tiers.
    """
    if str(res.get("site_type") or "").strip().lower() != "tower":
        return False
    if res.get("cell_equipment") is not True:
        return False
    if db_backed and db_backed_naip_tower_skip_nearmap_reason(
        res, db_backed=True
    ):
        return False
    return True


def naip_age_blocks_early_stop(
    res: dict,
    naip_age_years: float | None,
    *,
    max_age_years: float | None = None,
    high_conf_override: float | None = None,
) -> bool:
    """True when stale NAIP must not early-stop Nearmap (rooftop equipment risk).

    Towers can still stop on old NAIP. Rooftops on imagery older than
    NAIP_MAX_AGE_YEARS continue to Nearmap, unless confidence is at/above
    NAIP_AGE_HIGH_CONF_OVERRIDE (definitive asset ID on NAIP alone).
    Early-stop still also requires tier_confident_stop (equipment decided).
    Note: classify_with_tiers skips Nearmap for rooftops only when NAIP
    cell is already locked; stale NAIP still continues via this gate.
    """
    if naip_age_years is None:
        return False
    try:
        age = float(naip_age_years)
    except (TypeError, ValueError):
        return False
    limit = NAIP_MAX_AGE_YEARS if max_age_years is None else float(max_age_years)
    if age <= limit:
        return False
    if not _is_rooftop(res):
        return False
    conf = normalize_confidence(res.get("site_confidence"))
    override = (
        NAIP_AGE_HIGH_CONF_OVERRIDE
        if high_conf_override is None
        else float(high_conf_override)
    )
    if conf is not None and conf >= override:
        return False
    return True


def _has_nearmap_context(res: dict) -> bool:
    """True when this result already used Nearmap (not NAIP-only rescue)."""
    tier = str(res.get("nearmap_tier") or "").strip().lower()
    if tier in {"full", "wide_aoi", "vert_only"}:
        return True
    views = str(res.get("nearmap_views") or "").strip()
    if views:
        return True
    return has_locked_oblique_asset_box(res)


def _nearmap_empty_without_cell(res: dict) -> bool:
    """Full Nearmap other/unclear with no confirmed cell — skip Claude."""
    from enrichment.cost_policy import nearmap_empty_without_cell

    return nearmap_empty_without_cell(
        res.get("site_type"),
        res.get("nearmap_tier"),
        res.get("cell_equipment"),
    )


def escalation_reason(res: dict) -> str | None:
    """Why a Gemini result should escalate to Claude; None if no escalation."""
    site = str(res.get("site_type") or "").strip().lower()
    conf = normalize_confidence(res.get("site_confidence"))
    # Carrier-claimed / high-trust: Gemini empty on Nearmap still needs Claude
    # unless the pack locked other at >= 0.90. Medium-trust empties skip.
    if site in {"other", "unclear"} and _has_nearmap_context(res):
        if gemini_confidence_locks_claude(res):
            return None
        if _nearmap_empty_without_cell(res):
            if (
                source_expects_asset(res)
                and conf is not None
                and conf < EMPTY_CHIP_LOCK_CONF
            ):
                return "nearmap_claimed_site_empty"
            return None
        if conf is not None and conf >= CLAUDE_ESCALATE_MIN_SITE_CONF:
            return "nearmap_claimed_site_empty"
        return None
    if conf is None or conf < CLAUDE_ESCALATE_MIN_SITE_CONF:
        return None
    # Rooftops: Claude is for Nearmap HVAC vs antenna, not a NAIP substitute.
    if _is_rooftop(res):
        if not _has_nearmap_context(res):
            return None
        if res.get("cell_equipment") is not True:
            return "rooftop_cell_unconfirmed"
        if not _rooftop_cell_confirmed(res):
            return "rooftop_low_cell_confidence"
    elif res.get("cell_equipment") is False:
        # Towers (and non-rooftop): definitive no-gear — skip Claude.
        return None
    # Gemini already locked the call — don't overwrite with Claude.
    if gemini_confidence_locks_claude(res):
        return None
    if site in {"other", "unclear"}:
        return None
    if site_confidence_band(res) == "low":
        return "low_confidence"
    return None


def _brief_pass_result(res: dict) -> str:
    """Compact site_type/confidence for step-done lines."""
    site = str(res.get("site_type") or "—").strip() or "—"
    subtype = res.get("tower_subtype")
    if site == "tower" and subtype not in (None, "", "nan"):
        site = f"{site}/{subtype}"
    conf = res.get("site_confidence")
    try:
        conf_s = f"{float(conf):.2f}"
    except (TypeError, ValueError):
        conf_s = "—"
    return f"{site} conf={conf_s}"


def _step_done(step: str, detail: str | None = None) -> None:
    """Operator progress: mark a per-site pipeline step complete."""
    if detail:
        _out(f"         {step} — done ({detail})", important=True)
    else:
        _out(f"         {step} — done", important=True)


def _classify_pass(
    provider: str,
    clients: dict,
    views: list,
    prompt: str,
    input_confidence: str,
    *,
    screen: bool = False,
) -> dict:
    """One Gemini/Claude classify pass; optional lite screen then Flash confirm."""
    gemini_model = None
    if provider == "gemini":
        gemini_model = GEMINI_SCREEN_MODEL if screen else GEMINI_MODEL
    res = classify_site(
        provider,
        clients,
        views,
        prompt=prompt,
        gemini_model=gemini_model,
        image_max_px=SCREEN_IMAGE_MAX_PX if screen else None,
    )
    if (
        screen
        and provider == "gemini"
        and GEMINI_SCREEN_MODEL != GEMINI_MODEL
        and needs_flash_confirm(res)
    ):
        res = classify_site(
            provider, clients, views, prompt=prompt, gemini_model=GEMINI_MODEL
        )
        _step_done("Flash confirm", _brief_pass_result(res))
    res["input_confidence"] = normalize_input_confidence(input_confidence)
    res = maybe_recheck_equipment(provider, clients, res, views, input_confidence)
    res["input_confidence"] = normalize_input_confidence(input_confidence)
    return res


def _osm_features(lat: float, lon: float) -> dict:
    """Nearby OSM building/tower flags. Fail-open to ok=False."""
    empty = {
        "ok": False,
        "has_building": False,
        "has_tower_or_mast": False,
        "communication_tower": False,
    }
    try:
        from enrichment.osm_prefilter import lookup_osm_features
    except Exception:
        return empty
    try:
        return lookup_osm_features(lat, lon)
    except Exception:
        return empty


def obliques_settled(res: dict) -> bool:
    """True when Vert + one oblique already locks the call (skip the other obliques).

    Locked tower (Gemini solo bar, cell true), locked empty lot (``other`` at
    the empty lock), or rooftop gear localized on an oblique at the empty-lock
    cell confidence. Everything else — HVAC-only roofs, weak or unclear
    calls — buys the remaining obliques, since facade gear can face away
    from the first camera.
    """
    site = str(res.get("site_type") or "").strip().lower()
    conf = normalize_confidence(res.get("site_confidence")) or 0.0
    if site == "tower":
        return res.get("cell_equipment") is True and conf >= GEMINI_SOLO_CELL_CONF
    if site == "other":
        return res.get("cell_equipment") is not True and conf >= EMPTY_CHIP_LOCK_CONF
    if site == "rooftop":
        cell_conf = normalize_confidence(res.get("cell_equipment_confidence")) or 0.0
        return (
            res.get("cell_equipment") is True
            and cell_conf >= EMPTY_CHIP_LOCK_CONF
            and has_locked_oblique_asset_box(res)
        )
    return False


def locked_gemini_tower_skip_nearmap_reason(
    res: dict, *, db_backed: bool = False, osm_tower: bool = False
) -> str | None:
    """Skip-Nearmap label for a locked Gemini tower, or None.

    FCC/TowerSource (``db_backed``) is a DB-hit. OSM is not — it uses a
    separate string so terminals and metrics do not count it as a database match.
    """
    if not should_skip_claude_for_gemini_tower(res):
        return None
    if rooftop_requires_nearmap_tiers(res):
        return None
    if db_backed:
        return f"DB-hit Gemini tower conf>={GEMINI_SOLO_CELL_CONF:g}"
    if osm_tower:
        return "OSM tower + Gemini lock — no obliques"
    return None


def classify_with_tiers(lat: float, lon: float, img: Image.Image | None,
                        provider: str, clients: dict, prompt: str,
                        input_confidence: str,
                        build_views,
                        naip_age_years: float | None = None,
                        db_backed: bool = False,
                        osm_tower: bool = False) -> tuple[dict, dict, str | None, str, list]:
    """NAIP screen, then Nearmap Vert+obliques unless a cheaper lock applies.

    Skip paid imagery when: a DB-backed NAIP tower already decided cell at
    medium+ conf; NAIP rooftop cell is locked at the Gemini solo bar; NAIP
    is empty and OSM has no building/tower; an OSM communication tower plus
    Gemini lock; or there is no API key. Remaining claimed sites and weaker
    rooftops still buy Vert+obliques.
    """
    nearmap_views: dict = {}
    nearmap_date = None

    views = build_views({})
    _step_done("NAIP")
    res = _classify_pass(
        provider, clients, views, prompt, input_confidence, screen=True
    )
    _step_done("classify (NAIP)", _brief_pass_result(res))
    stamp_naip_screen(res)

    skip = skip_nearmap_after_naip_reason(
        res, db_backed=db_backed, osm_tower=False, naip_age_years=naip_age_years
    )
    if skip:
        _step_done("Nearmap skipped", skip)
        return res, nearmap_views, nearmap_date, "naip_only", views

    osm_info = None
    if not db_backed:
        osm_info = _osm_features(lat, lon)
        if osm_info.get("communication_tower"):
            osm_tower = True
            _step_done("OSM", "communication tower nearby")
        elif osm_info.get("ok") and not (
            osm_info.get("has_building") or osm_info.get("has_tower_or_mast")
        ):
            _step_done("OSM", "no building/tower")
        skip = skip_nearmap_after_naip_reason(
            res,
            db_backed=False,
            osm_tower=osm_tower,
            osm_info=osm_info,
            naip_age_years=naip_age_years,
        )
        if skip:
            _step_done("Nearmap skipped", skip)
            return res, nearmap_views, nearmap_date, "naip_only", views

    if rooftop_requires_nearmap_tiers(res):
        _step_done(
            "Nearmap required",
            "rooftop — Vert+obliques for cellular confirmation",
        )
    else:
        _step_done(
            "Nearmap required",
            "claimed site — Vert+obliques (NAIP cannot rule out gear)",
        )

    if not NEARMAP_API_KEY:
        return res, nearmap_views, nearmap_date, "naip_only", views

    vert_views, vert_date = fetch_nearmap_views(lat, lon, views=["Vert"])
    nearmap_views.update(vert_views)
    nearmap_date = vert_date or nearmap_date
    if not nearmap_views:
        _step_done("Nearmap vert", "no coverage")
        return res, nearmap_views, nearmap_date, "no_coverage", views
    _step_done("Nearmap vert")

    def classify_pack() -> tuple[dict, list]:
        pack_views = build_views(nearmap_views)
        out = _classify_pass(
            provider, clients, pack_views, prompt, input_confidence, screen=False
        )
        out = gate_weak_rooftop_cell_claim(out)
        out = gate_weak_stealth_tower_claim(out)
        return out, pack_views

    missing = [v for v in OBLIQUE_VIEWS if v not in nearmap_views]
    # Staggered obliques: buy the first one, and the rest only when the
    # Vert + first-oblique call is not already locked (saves ~1/3 of a pack).
    staggered = NEARMAP_STAGGER_OBLIQUES and len(missing) > 1
    first, rest = (missing[:1], missing[1:]) if staggered else (missing, [])
    if first:
        oblique_views, ob_date = fetch_nearmap_views(lat, lon, views=first)
        nearmap_views.update(oblique_views)
        nearmap_date = ob_date or nearmap_date
    if rest and not any(v in nearmap_views for v in first):
        # First oblique had no coverage: nothing to judge yet, buy the rest now.
        oblique_views, ob_date = fetch_nearmap_views(lat, lon, views=rest)
        nearmap_views.update(oblique_views)
        nearmap_date = ob_date or nearmap_date
        rest = []
    res, views = classify_pack()
    if rest:
        if obliques_settled(res):
            _step_done("Nearmap obliques", f"{first[0]} only — call locked, skip {', '.join(rest)}")
        else:
            oblique_views, ob_date = fetch_nearmap_views(
                lat, lon, views=rest, purpose="oblique_extra"
            )
            if oblique_views:
                nearmap_views.update(oblique_views)
                nearmap_date = ob_date or nearmap_date
                res, views = classify_pack()
    has_obliques = any(v in nearmap_views for v in OBLIQUE_VIEWS)
    _step_done("Nearmap obliques", None if has_obliques else "no coverage")
    _step_done("classify (Nearmap combined)", _brief_pass_result(res))
    return (
        res,
        nearmap_views,
        nearmap_date,
        "full" if has_obliques else "vert_only",
        views,
    )


def scout_candidates(provider: str, clients: dict, label: str,
                     img: Image.Image) -> list[dict]:
    """Ask the vision model to propose candidate regions on one top-down image."""
    views = [(label, img)]
    gemini_model = GEMINI_SCREEN_MODEL if provider == "gemini" else None
    res = classify_site(
        provider,
        clients,
        views,
        scan=True,
        gemini_model=gemini_model,
        image_max_px=SCREEN_IMAGE_MAX_PX,
    )
    return res.get("candidates") or []


def _anchor_candidates() -> list[dict]:
    """Default crops around the recorded coordinate (chip center) and the
    band just below center, where assets often sit when coords are approximate."""
    return [
        {"box_2d": [350, 350, 650, 650],
         "reason": "coordinate anchor (center)"},
        {"box_2d": [480, 380, 680, 620],
         "reason": "coordinate anchor (just below center)"},
    ]


def select_zoom_candidates(
    scouted: list[dict],
    *,
    max_crops: int = ZOOM_MAX_CANDIDATES,
) -> list[dict]:
    """Prefer scout boxes; fill remaining slots with pin-center anchors."""
    merged: list[dict] = []
    seen: set[tuple] = set()

    def _add(cands: list[dict]) -> None:
        for cand in cands:
            box = _valid_box(cand.get("box_2d"))
            if box is None:
                continue
            key = tuple(box)
            if key in seen:
                continue
            seen.add(key)
            merged.append(cand)
            if len(merged) >= max_crops:
                return

    _add(list(scouted or []))
    if len(merged) < max_crops:
        _add(_anchor_candidates())
    if not merged:
        _add([{"box_2d": b, "reason": "grid sweep"} for b in _grid_boxes()])
    return merged[:max_crops]


def build_zoom_views(asset_id: str, source_label: str, source_img: Image.Image,
                     candidates: list[dict]) -> list[tuple[str, Image.Image]]:
    """Turn scout candidates into magnified zoom crops; save each to chips/."""
    zoom_views = []
    seen = set()
    for i, cand in enumerate(candidates[:ZOOM_MAX_CANDIDATES], start=1):
        box = _valid_box(cand.get("box_2d"))
        if box is None:
            continue
        key = tuple(box)
        if key in seen:
            continue
        seen.add(key)
        crop = _crop_zoom(source_img, box)
        reason = (cand.get("reason") or "candidate").replace("\n", " ")[:80]
        path = CHIP_DIR / f"{asset_id}_zoom_{i}.jpg"
        crop.save(path, quality=92)
        zoom_views.append((f"Zoom crop {i} ({reason})", crop))
    return zoom_views


def run_zoom_stage(provider: str, clients: dict, asset_id: str,
                   context_views: list[tuple[str, Image.Image]],
                   source_label: str, source_img: Image.Image,
                   max_crops: int = ZOOM_MAX_CANDIDATES) -> tuple[dict, int]:
    """Scout + magnify + re-classify. Returns (result dict, zoom crop count)."""
    scouted = scout_candidates(provider, clients, source_label, source_img)
    if not scouted:
        _out(f"  [{asset_id}] scout found no extra candidates")
    candidates = select_zoom_candidates(scouted, max_crops=max_crops)

    zoom_views = build_zoom_views(asset_id, source_label, source_img, candidates)
    if not zoom_views:
        return {"site_type": "unclear", "site_confidence": 0.0,
                "site_evidence": "Zoom stage could not build valid crops."}, 0

    context = context_views[:1] if context_views else []
    res = classify_site(
        provider, clients, context + zoom_views, prompt=_active_zoom_prompt())
    res["classification_stage"] = "zoom"
    return res, len(zoom_views)


def classify_with_routing(provider: str, clients: dict, views: list,
                          prompt: str, input_confidence: str,
                          *, escalate: bool = True, screen: bool = False
                          ) -> tuple[dict, str, str | None, str | None]:
    """Run primary classification; optionally escalate Gemini -> Claude.

    Pass escalate=False for intermediate imagery stages so Gemini is fully
    exhausted (NAIP/Nearmap/zoom) before a single final Claude call.
    ``screen=True`` uses GEMINI_SCREEN_MODEL then Flash-confirm when needed.
    """
    primary_model = provider
    res = _classify_pass(
        provider, clients, views, prompt, input_confidence, screen=screen
    )

    escalation_model = None
    escalation_reason_str = None
    if (escalate and BIFURCATED_AI and not GEMINI_ONLY
            and provider == "gemini"):
        res, escalation_model, escalation_reason_str = maybe_escalate_to_claude(
            res, clients, views, prompt, input_confidence, allow=True
        )
    return res, primary_model, escalation_model, escalation_reason_str


def cheap_second_opinion_disagrees(
    res: dict, clients: dict, views: list, prompt: str
) -> bool:
    """True when Flash (or a second Gemini pass) calls a positive vs other/unclear.

    If the last call was already GEMINI_MODEL, type alone is not disagreement.
    """
    site = str(res.get("site_type") or "").strip().lower()
    if site not in {"other", "unclear"}:
        return False
    if confident_no_asset(res):
        return False
    if "gemini" not in clients or clients.get("gemini") is None:
        return False
    # A fallback answer was requested as GEMINI_MODEL: do not pay a second call.
    last_model = str(res.get("model_requested") or res.get("model") or "").strip()
    if last_model == GEMINI_MODEL:
        return False
    second = classify_site(
        "gemini", clients, views, prompt=prompt, gemini_model=GEMINI_MODEL
    )
    return str(second.get("site_type") or "").strip().lower() in _positive_site_types()


def should_attempt_claude_escalation(res: dict) -> bool:
    """True when Claude (or a Gemini second opinion) is worth spending.

    Low-confidence NAIP ``unclear`` and NAIP-only rooftops are holdouts, not
    Claude jobs. Full Nearmap other/unclear with no cell also skips the
    cheap second-opinion path — Claude does not recover those.
    """
    if escalation_reason(res):
        return True
    if _nearmap_empty_without_cell(res):
        return False
    site = str(res.get("site_type") or "").strip().lower()
    if site not in {"other", "unclear"} or confident_no_asset(res):
        return False
    if site == "unclear":
        conf = normalize_confidence(res.get("site_confidence"))
        return conf is not None and conf >= TIER_CONF_MEDIUM
    return True


def maybe_escalate_to_claude(res: dict, clients: dict, views: list, prompt: str,
                             input_confidence: str,
                             *, allow: bool
                             ) -> tuple[dict, str | None, str | None]:
    """Single late Claude escalation after all Gemini imagery stages."""
    if not allow:
        return res, None, None
    if not should_attempt_claude_escalation(res):
        return res, None, None
    reason = escalation_reason(res)
    if not reason:
        if cheap_second_opinion_disagrees(res, clients, views, prompt):
            reason = "second_opinion_disagree"
        else:
            return res, None, None
    escalated = classify_site(
        "claude", clients, views, prompt=prompt,
        claude_model=CLAUDE_ESCALATION_MODEL)
    escalated = maybe_recheck_equipment(
        "claude", clients, escalated, views, input_confidence)
    _step_done("escalate (Claude)", _brief_pass_result(escalated))
    return escalated, "claude", reason
