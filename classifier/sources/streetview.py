"""Google Street View Static API street-level views (disabled by default).

LICENSING: Google Maps Platform terms restrict caching, storage and use of
Street View imagery (including feeding it to third-party ML models and
retaining it beyond short-term caching). These terms must be reviewed and
approved before any production use. Until then this source stays off unless
``GOOGLE_STREETVIEW_ENABLED=1`` AND ``GOOGLE_MAPS_API_KEY`` are both set.

Flow: the free metadata endpoint finds the nearest outdoor panorama within
``STREETVIEW_RADIUS_M``; if its status is ``OK`` we aim the camera from the
panorama location at the site and fetch a 640x640 image (billable). With
``STREETVIEW_VIEWS=2`` a second image is taken 15 degrees higher for tall
buildings. Endpoints and parameters follow
https://developers.google.com/maps/documentation/streetview/request-streetview
and .../streetview/metadata ("metadata requests are available at no charge").
"""

from __future__ import annotations

import logging
from pathlib import Path

from classifier.sources import base
from envutil import env_flag, env_float, env_int, env_str

NAME = "streetview"
METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"
IMAGE_SIZE = "640x640"   # Static API maximum without a premium plan
TALL_PITCH_STEP = 15.0

logger = logging.getLogger(__name__)


def api_key() -> str:
    return env_str("GOOGLE_MAPS_API_KEY")


def enabled() -> bool:
    return env_flag("GOOGLE_STREETVIEW_ENABLED", False)


def available() -> tuple[bool, str]:
    if not enabled():
        return False, "GOOGLE_STREETVIEW_ENABLED is not 1 (licensing review pending)"
    if not api_key():
        return False, "GOOGLE_MAPS_API_KEY is not set"
    return True, ""


def radius_m() -> float:
    return max(1.0, env_float("STREETVIEW_RADIUS_M", 50.0))


def pitch() -> float:
    return max(-90.0, min(90.0, env_float("STREETVIEW_PITCH", 25.0)))


def fov() -> float:
    return max(10.0, min(120.0, env_float("STREETVIEW_FOV", 60.0)))


def max_views() -> int:
    return max(0, min(2, env_int("STREETVIEW_VIEWS", 1)))


def heading_to_site(pano_lat: float, pano_lon: float, lat: float, lon: float) -> float:
    """Camera heading (0 = north, clockwise) from the panorama toward the site."""
    return round(base.initial_bearing_deg(pano_lat, pano_lon, lat, lon), 1)


def _metadata(lat: float, lon: float) -> dict:
    params = {"location": f"{lat:.7f},{lon:.7f}", "radius": f"{radius_m():g}",
              "source": "outdoor", "key": api_key()}
    resp = base.http_get(METADATA_URL, params=params, billable=False)
    if not resp.ok:
        logger.warning("Street View metadata HTTP %s", resp.status_code)
        return {}
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def _image_cache_path(pano_id: str, heading: float, pitch_deg: float, fov_deg: float) -> Path | None:
    root = base.cache_dir(NAME)
    if root is None:
        return None
    stem = f"{heading:.1f}_{pitch_deg:g}_{fov_deg:g}"
    return root / base.safe_key(pano_id) / f"{base.safe_key(stem)}.jpg"


def _fetch_image(pano_id: str, heading: float, pitch_deg: float, fov_deg: float):
    path = _image_cache_path(pano_id, heading, pitch_deg, fov_deg)
    data = base.cache_read(path)
    if data is not None:
        img = base.decode_image(data)
        if img is not None:
            return img
    params = {"size": IMAGE_SIZE, "pano": pano_id, "heading": f"{heading:.1f}",
              "pitch": f"{pitch_deg:g}", "fov": f"{fov_deg:g}",
              "return_error_code": "true", "key": api_key()}
    resp = base.http_get(IMAGE_URL, params=params, billable=True)
    if not resp.ok:
        logger.warning("Street View image HTTP %s for pano %s", resp.status_code, pano_id)
        return None
    img = base.decode_image(resp.content, (resp.headers or {}).get("Content-Type"))
    if img is None or base.is_blank_image(img):
        return None
    base.cache_write(path, resp.content)
    return img


def label_for(captured: str | None, distance_m: float, approach: float, heading: float,
              pitch_deg: float, *, tall: bool = False) -> str:
    when = f" {captured}" if captured else ""
    extra = ", tilted up for upper floors and roofline" if tall else ""
    return (f"Street-level photo (Google Street View{when}, camera {distance_m:.0f} m "
            f"{base.compass_abbrev(approach)} of site, facing {base.compass_abbrev(heading)} "
            f"toward site, pitch {pitch_deg:g} deg{extra})")


def fetch(lat: float, lon: float, *, state: str | None = None) -> list[base.SupplementalView]:
    del state
    ok, _reason = available()
    limit = max_views()
    if not ok or limit <= 0:
        return []
    meta = _metadata(lat, lon)
    if meta.get("status") != "OK" or not meta.get("pano_id"):
        if meta.get("status") not in (None, "OK", "ZERO_RESULTS"):
            logger.warning("Street View metadata status %s", meta.get("status"))
        return []
    pano_id = str(meta["pano_id"])
    loc = meta.get("location") or {}
    try:
        pano_lat, pano_lon = float(loc["lat"]), float(loc["lng"])
    except (KeyError, TypeError, ValueError):
        pano_lat, pano_lon = lat, lon
    distance = base.haversine_m(pano_lat, pano_lon, lat, lon)
    heading = heading_to_site(pano_lat, pano_lon, lat, lon) if distance >= 1.0 else 0.0
    approach = (heading + 180.0) % 360.0
    captured = str(meta["date"]) if meta.get("date") else None
    fov_deg = fov()
    pitches = [pitch()]
    if limit >= 2:
        pitches.append(min(90.0, pitch() + TALL_PITCH_STEP))
    views = []
    for index, pitch_deg in enumerate(pitches):
        img = _fetch_image(pano_id, heading, pitch_deg, fov_deg)
        if img is None:
            continue
        views.append(base.SupplementalView(
            label=label_for(captured, distance, approach, heading, pitch_deg, tall=index > 0),
            image=img,
            source=NAME,
            captured=captured,
            meta={
                "pano_id": pano_id,
                "distance_m": round(distance, 1),
                "heading": heading,
                "pitch": pitch_deg,
                "fov": fov_deg,
                "copyright": meta.get("copyright"),
                "service": "Google Street View Static API",
            },
        ))
    return views
