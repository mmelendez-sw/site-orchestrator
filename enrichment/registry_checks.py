"""Cheap corroboration for registry matches the distance rules leave unsettled.

``address_match``: the registry's structure address against the Salesforce
site address (free; the columns come back with the proximity rows).
``naip_registry_check``: a free NAIP chip centred on the registry coordinates,
read by one Gemini call (``REGISTRY_NAIP_CHECK=1``). Neither writes to
Salesforce: both annotate review_queue.csv until calibrated against reviews.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from enrichment.constants import (
    MATCH_SOURCE_COMPETITOR,
    MATCH_SOURCE_FCC,
    MATCH_SOURCE_TOWERSOURCE,
)
from envutil import env_float, env_str

logger = logging.getLogger(__name__)

# (street, city, zip) keys per registry row.
_ADDRESS_KEYS = {
    MATCH_SOURCE_FCC: ("Structure_Street_Address", "Structure_City", "Structure_ZIP_Code"),
    MATCH_SOURCE_TOWERSOURCE: ("street1", "city", "postal_code"),
    MATCH_SOURCE_COMPETITOR: ("address", "city", "postal_code"),
}
_PHRASES = (
    (r"\bcounty (road|rd|route|rte|highway|hwy)\b", "cr"),
    (r"\bstate (road|rd|route|rte|highway|hwy)\b", "sr"),
    (r"\b(us|u s) (highway|hwy|route|rte)\b", "us"),
    (r"\binterstate\b", "i"),
)
_SUFFIX = {
    "street": "st", "road": "rd", "avenue": "ave", "av": "ave", "highway": "hwy",
    "drive": "dr", "lane": "ln", "boulevard": "blvd", "route": "rt", "rte": "rt",
    "parkway": "pkwy", "court": "ct", "circle": "cir", "place": "pl", "trail": "trl",
    "turnpike": "tpke", "terrace": "ter", "mountain": "mtn", "mount": "mt",
}
# Words that say where on a street, not which street.
_NOISE = frozenset(
    "n s e w ne nw se sw north south east west st rd ave hwy dr ln blvd rt pkwy ct "
    "cir pl trl tpke ter way pike of and the at near cor corner approx approximately "
    "mi mile miles ft feet off from side site tower lot unit suite ste".split()
)
_ROUTE_WORDS = frozenset({"cr", "sr", "us", "i", "rt", "hwy", "fm", "rr"})


def _tokens(street: str) -> tuple[str, list[str]]:
    """(house number or '', significant street tokens) for one street line."""
    text = re.sub(r"[^a-z0-9 ]", " ", str(street or "").lower())
    for pattern, repl in _PHRASES:
        text = re.sub(pattern, repl, text)
    words = [_SUFFIX.get(w, w) for w in text.split()]
    number = ""
    if words and re.fullmatch(r"\d+[a-z]?", words[0]) and len(words) > 1:
        number, words = re.sub(r"\D", "", words[0]), words[1:]
    keep: list[str] = []
    for i, word in enumerate(words):
        route_number = word.isdigit() and i > 0 and words[i - 1] in _ROUTE_WORDS
        if route_number:
            keep.append(f"{words[i - 1]}{word}")
        elif word not in _NOISE and not word.isdigit() and len(word) >= 3:
            keep.append(word)
    return number, keep


def _zip5(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:5] if len(digits) >= 5 else ""


def compare_addresses(site_street: str, site_zip: Any, reg_street: str, reg_zip: Any) -> str:
    """'number_and_street', 'street', 'zip_only', 'none', or '' (either side blank)."""
    s_num, s_tok = _tokens(site_street)
    r_num, r_tok = _tokens(reg_street)
    if not (s_tok or s_num) or not (r_tok or r_num):
        return ""
    shared = set(s_tok) & set(r_tok)
    if shared and s_num and s_num == r_num:
        return "number_and_street"
    if shared:
        return "street"
    if _zip5(site_zip) and _zip5(site_zip) == _zip5(reg_zip):
        return "zip_only"
    return "none"


_RANK = {"number_and_street": 4, "street": 3, "zip_only": 2, "none": 1, "": 0}


def address_match(site: dict[str, Any], hit) -> dict[str, str]:
    """Best address agreement across the registry rows behind one structure."""
    best = {"address_match": "", "registry_address": ""}
    if hit is None:
        return best
    for member in getattr(hit, "members", ()) or (hit,):
        keys = _ADDRESS_KEYS.get(member.source)
        raw = member.raw or {}
        if not keys or not raw.get(keys[0]):
            continue
        level = compare_addresses(
            site.get("Site_Street__c") or "", site.get("Site_Zip_Code__c"), raw[keys[0]], raw.get(keys[2])
        )
        if _RANK[level] > _RANK[best["address_match"]]:
            city = str(raw.get(keys[1]) or "").strip()
            best = {"address_match": level, "registry_address": f"{raw[keys[0]]}, {city}".strip(", ")}
    return best


# ------------------------------- NAIP check ---------------------------------

NAIP_CHECK_CHIP_M = env_float("REGISTRY_NAIP_CHIP_M", 120)
_RING_M = 15.0
_SCHEMA = {
    "type": "object",
    "properties": {
        "structure_at_ring": {"type": "string", "enum": ["tower", "other_structure", "none", "unclear"]},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    },
    "required": ["structure_at_ring", "confidence", "evidence"],
}
_PROMPT = (
    "NAIP aerial image, about 0.6 m per pixel, {chip_m:g} m across. The red ring "
    "(15 m radius) is centred on where a tower registry places a structure; a blue "
    "dot, if shown, is where our sales record places the site. Is a "
    "telecommunications tower (lattice, monopole, guyed mast, stealth) or its "
    "fenced equipment compound inside the red ring? Evidence: a tower base with a "
    "long thin shadow, guy-wire anchor paths, or a fenced gravel pad with "
    "shelters or cabinets. Answer tower, other_structure (a building, water tank, "
    "billboard, silo), none (empty land, road, field) or unclear (cloud, too "
    "blurry). Confidence is 0-1. Evidence is one short sentence."
)


def _marked_chip(img, geo: dict, pin: tuple[float, float] | None):
    from PIL import ImageDraw
    from pyproj import Transformer

    img = img.copy().convert("RGB")
    w, h = img.size
    px_per_m = w / float(geo["chip_m"])
    draw = ImageDraw.Draw(img)
    r = _RING_M * px_per_m
    draw.ellipse((w / 2 - r, h / 2 - r, w / 2 + r, h / 2 + r), outline=(255, 0, 0), width=2)
    if pin is not None:
        x, y = Transformer.from_crs("EPSG:4326", geo["crs"], always_xy=True).transform(pin[1], pin[0])
        px = (x - geo["x_min"]) * px_per_m
        py = (geo["y_max"] - y) * px_per_m
        if 0 <= px < w and 0 <= py < h:
            draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill=(0, 120, 255), outline=(255, 255, 255))
    return img


def naip_registry_check(
    lat: float,
    lng: float,
    *,
    pin: tuple[float, float] | None = None,
    save_to: Path | None = None,
    client=None,
    chip_m: float = NAIP_CHECK_CHIP_M,
) -> dict[str, Any]:
    """NAIP chip at the registry point, read by Gemini. Never raises."""
    out = {"naip_check": "", "naip_check_conf": "", "naip_check_evidence": "",
           "naip_check_date": "", "naip_check_chip": ""}
    try:
        from classifier import asset_classifier as ac
        from classifier.imagery import fetch_chip
        from classifier.llm import _views_to_gemini_contents

        img, meta, geo = fetch_chip(lat, lng, chip_m)
        if img is None:
            out["naip_check"] = "no_imagery"
            return out
        marked = _marked_chip(img, geo, pin)
        if save_to is not None:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            marked.save(save_to, quality=90)
            out["naip_check_chip"] = str(save_to)
        out["naip_check_date"] = str((meta or {}).get("naip_date") or (meta or {}).get("image_date") or "")
        if client is None:
            import os

            from google import genai

            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        model = env_str("REGISTRY_NAIP_MODEL", ac.GEMINI_MODEL)
        contents = _views_to_gemini_contents([("NAIP", marked.resize((marked.width * 3, marked.height * 3)))],
                                             _PROMPT.format(chip_m=chip_m))
        res = ac._call_gemini_json(client, contents, _SCHEMA, model=model)
        out["naip_check"] = str(res.get("structure_at_ring") or "unclear")
        out["naip_check_conf"] = res.get("confidence", "")
        out["naip_check_evidence"] = str(res.get("evidence") or "")[:300]
    except Exception as exc:  # noqa: BLE001 — corroboration is optional
        logger.warning("NAIP registry check failed at %.6f,%.6f: %s", lat, lng, exc)
        out["naip_check"] = "error"
        out["naip_check_evidence"] = str(exc)[:300]
    return out
