"""Mapillary street-level photos (Graph API v4, free with an access token).

Street-level photos show rooftop antennas and towers from the side, which
NAIP cannot. We search a small bbox around the site, keep non-panoramic
images whose camera points at the site, and download the 2048 px thumbnail.

Field names (``thumb_2048_url``, ``captured_at`` in epoch ms,
``computed_compass_angle``, ``computed_geometry``, ``is_pano``) and the bbox
format (minLon,minLat,maxLon,maxLat; area must be < 0.01 square degrees)
follow https://www.mapillary.com/developer/api-documentation. The token is
sent in the documented ``Authorization: OAuth <token>`` header so it never
appears in a URL.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from classifier.sources import base
from envutil import env_float, env_int, env_str

NAME = "mapillary"
SEARCH_URL = "https://graph.mapillary.com/images"
FIELDS = ("id,thumb_2048_url,captured_at,compass_angle,computed_compass_angle,"
          "computed_geometry,geometry,is_pano")
MIN_DISTANCE_M = 8.0
MIN_DIRECTION_SEPARATION_DEG = 60.0
ALIGNMENT_BUCKET_DEG = 10.0

logger = logging.getLogger(__name__)


def token() -> str:
    return env_str("MAPILLARY_ACCESS_TOKEN")


def radius_m() -> float:
    return max(10.0, env_float("MAPILLARY_RADIUS_M", 60.0))


def max_heading_diff() -> float:
    return max(1.0, min(180.0, env_float("MAPILLARY_MAX_HEADING_DIFF", 50.0)))


def max_views() -> int:
    return max(0, env_int("MAPILLARY_MAX_VIEWS", 2))


def search_cache_days() -> float:
    return env_float("MAPILLARY_SEARCH_CACHE_DAYS", 30.0)


def available() -> tuple[bool, str]:
    if not token():
        return False, "MAPILLARY_ACCESS_TOKEN is not set"
    return True, ""


def _search_cache_path(lat: float, lon: float, radius: float) -> Path | None:
    root = base.cache_dir(NAME)
    if root is None:
        return None
    return root / "search" / f"{base.safe_key(f'{lat:.5f}_{lon:.5f}_{radius:g}')}.json"


def _image_cache_path(image_id: str) -> Path | None:
    root = base.cache_dir(NAME)
    if root is None:
        return None
    return root / "images" / f"{base.safe_key(image_id)}.jpg"


def _search(lat: float, lon: float, radius: float, *,
            use_cache: bool = True) -> tuple[list[dict], bool]:
    """(images, served_from_cache)."""
    path = _search_cache_path(lat, lon, radius)
    cached = base.cache_read(path, max_age_days=search_cache_days()) if use_cache else None
    if cached is not None:
        try:
            return list(json.loads(cached.decode("utf-8")).get("data") or []), True
        except (ValueError, AttributeError):
            pass
    minlon, minlat, maxlon, maxlat = base.lonlat_bbox(lat, lon, radius)
    params = {
        "fields": FIELDS,
        "bbox": f"{minlon:.7f},{minlat:.7f},{maxlon:.7f},{maxlat:.7f}",
        "limit": 100,
    }
    resp = base.http_get(SEARCH_URL, params=params,
                         headers={"Authorization": f"OAuth {token()}"})
    if not resp.ok:
        logger.warning("Mapillary search HTTP %s", resp.status_code)
        return [], False
    payload = resp.json()
    data = list(payload.get("data") or []) if isinstance(payload, dict) else []
    base.cache_write(path, json.dumps({"data": data}).encode("utf-8"))
    return data, False


def _point(image: dict) -> tuple[float, float] | None:
    for key in ("computed_geometry", "geometry"):
        geom = image.get(key) or {}
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                return float(coords[1]), float(coords[0])
            except (TypeError, ValueError):
                continue
    return None


def _heading(image: dict) -> float | None:
    for key in ("computed_compass_angle", "compass_angle"):
        value = image.get(key)
        if value is None:
            continue
        try:
            return float(value) % 360.0
        except (TypeError, ValueError):
            continue
    return None


def candidates(lat: float, lon: float, images: list[dict], *,
               radius: float | None = None, max_diff: float | None = None) -> list[dict]:
    """Images that look at the site, ranked best first.

    Kept: not panoramic, MIN_DISTANCE_M..radius from the site, camera heading
    within ``max_diff`` of the bearing image->site. Ranked by heading
    alignment (10-degree buckets), then newest, then nearest, then id.
    """
    radius = radius_m() if radius is None else radius
    max_diff = max_heading_diff() if max_diff is None else max_diff
    out = []
    for image in images:
        if not isinstance(image, dict) or image.get("is_pano"):
            continue
        if not image.get("id") or not image.get("thumb_2048_url"):
            continue
        point = _point(image)
        heading = _heading(image)
        if point is None or heading is None:
            continue
        ilat, ilon = point
        dist = base.haversine_m(ilat, ilon, lat, lon)
        if dist < MIN_DISTANCE_M or dist > radius:
            continue
        to_site = base.initial_bearing_deg(ilat, ilon, lat, lon)
        diff = base.angle_diff_deg(heading, to_site)
        if diff > max_diff:
            continue
        try:
            captured_ms = float(image.get("captured_at") or 0)
        except (TypeError, ValueError):
            captured_ms = 0.0
        out.append({
            "id": str(image["id"]),
            "url": image["thumb_2048_url"],
            "distance_m": dist,
            "heading": heading,
            "bearing_to_site": to_site,
            # Where the camera stands, seen from the site (approach direction).
            "approach": (to_site + 180.0) % 360.0,
            "heading_diff": diff,
            "captured_ms": captured_ms,
        })
    out.sort(key=lambda c: (int(c["heading_diff"] // ALIGNMENT_BUCKET_DEG),
                            -c["captured_ms"], c["distance_m"], c["id"]))
    return out


def select(ranked: list[dict], limit: int) -> list[dict]:
    """Take up to ``limit``, preferring approach directions >= 60 degrees
    apart; fall back to the next-best ranked images if too few differ."""
    if limit <= 0:
        return []
    picked: list[dict] = []
    for cand in ranked:
        if len(picked) >= limit:
            break
        if all(base.angle_diff_deg(cand["approach"], p["approach"]) >= MIN_DIRECTION_SEPARATION_DEG
               for p in picked):
            picked.append(cand)
    for cand in ranked:
        if len(picked) >= limit:
            break
        if cand not in picked:
            picked.append(cand)
    return picked


def label_for(cand: dict, captured: str | None) -> str:
    when = f" {captured[:7]}" if captured else ""
    return (f"Street-level photo (Mapillary{when}, camera {cand['distance_m']:.0f} m "
            f"{base.compass_abbrev(cand['approach'])} of site, facing "
            f"{base.compass_abbrev(cand['heading'])} toward site)")


def _download(cand: dict) -> bytes | None:
    path = _image_cache_path(cand["id"])
    data = base.cache_read(path)
    if data is not None:
        return data
    resp = base.http_get(cand["url"])
    if not resp.ok:
        logger.info("Mapillary image %s HTTP %s", cand["id"], resp.status_code)
        return None
    data = resp.content
    if base.decode_image(data, (resp.headers or {}).get("Content-Type")) is None:
        return None
    base.cache_write(path, data)
    return data


def fetch(lat: float, lon: float, *, state: str | None = None) -> list[base.SupplementalView]:
    del state  # not used; Mapillary is global
    limit = max_views()
    if limit <= 0 or not token():
        return []
    radius = radius_m()
    found, from_cache = _search(lat, lon, radius)
    picked = select(candidates(lat, lon, found, radius=radius), limit)
    fresh: dict | None = None
    images: list[tuple[dict, Any]] = []
    for cand in picked:
        data = _download(cand)
        if data is None and from_cache:
            # Thumb URLs in a cached search can expire; refresh the search once.
            if fresh is None:
                fresh = {str(img.get("id")): img
                         for img in _search(lat, lon, radius, use_cache=False)[0]
                         if isinstance(img, dict)}
            new_url = (fresh.get(cand["id"]) or {}).get("thumb_2048_url")
            if new_url and new_url != cand["url"]:
                cand = dict(cand, url=new_url)
                data = _download(cand)
        img = base.decode_image(data) if data else None
        if img is not None:
            images.append((cand, img))
    views = []
    for cand, img in images:
        captured = base.iso_date_from_epoch_ms(cand["captured_ms"])
        views.append(base.SupplementalView(
            label=label_for(cand, captured),
            image=img,
            source=NAME,
            captured=captured,
            meta={
                "image_id": cand["id"],
                "distance_m": round(cand["distance_m"], 1),
                "heading": round(cand["heading"], 1),
                "bearing_to_site": round(cand["bearing_to_site"], 1),
                "heading_diff": round(cand["heading_diff"], 1),
                "service": "Mapillary",
            },
        ))
    return views
