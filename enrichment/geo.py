"""Distance helpers and Census geocode for pin vs street-address checks."""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from enrichment.constants import PIN_ADDRESS_MISMATCH_M, ROOFTOP_HOST_OFFSET_M
from envutil import env_flag, env_float, env_int

logger = logging.getLogger(__name__)

CENSUS_GEOCODER_URL = (
    "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
)
GEOCODE_TIMEOUT_S = env_float("GEOCODE_TIMEOUT_S", 5)


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in meters between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def geocode_address_enabled() -> bool:
    return env_flag("GEOCODE_ADDRESS", True)


def build_site_address(site: dict[str, Any]) -> str | None:
    """Compose a one-line address from Salesforce street/city/state/zip."""
    street = str(site.get("Site_Street__c") or "").strip()
    if not street:
        return None
    city = str(site.get("Site_City__c") or "").strip()
    state = str(site.get("Site_State__c") or "").strip()
    zipc = str(site.get("Site_Zip_Code__c") or "").strip()
    locality = ", ".join(p for p in (city, f"{state} {zipc}".strip()) if p)
    return f"{street}, {locality}" if locality else street


def parse_census_geocode_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull the first Census address match. None if empty or malformed."""
    if not payload:
        return None
    matches = (payload.get("result") or {}).get("addressMatches") or []
    if not isinstance(matches, list) or not matches:
        return None
    first = matches[0] if isinstance(matches[0], dict) else None
    if not first:
        return None
    coords = first.get("coordinates") or {}
    try:
        lng = float(coords.get("x"))
        lat = float(coords.get("y"))
    except (TypeError, ValueError):
        return None
    matched = str(first.get("matchedAddress") or "").strip()
    return {
        "lat": lat,
        "lng": lng,
        "matched": matched,
        "source": "census",
    }


def _fetch_census_oneline(text: str) -> dict[str, Any] | None:
    params = urllib.parse.urlencode(
        {
            "address": text,
            "benchmark": "Public_AR_Current",
            "format": "json",
        }
    )
    url = f"{CENSUS_GEOCODER_URL}?{params}"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "site-orchestrator/enrichment"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=GEOCODE_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        logger.info("Census geocode skipped: %s", exc)
        raise _GeocodeUnavailable(str(exc)) from exc
    if not isinstance(payload, dict):
        return None
    return parse_census_geocode_payload(payload)


def geocode_census(address: str) -> dict[str, Any] | None:
    """Geocode a US address via Census. Fail-open (None) on any error.

    Answers (including "no match") are cached across runs; transport
    failures are not, so the next run retries.
    """
    text = str(address or "").strip()
    if not text or not geocode_address_enabled():
        return None
    cache = _geocode_cache()
    hit, value = cache.get(text)
    if hit:
        return value
    try:
        result = _fetch_census_oneline(text)
    except _GeocodeUnavailable:
        return None
    cache.put(text, result)
    return result


# ------------------------------ geocode cache -------------------------------


class _GeocodeUnavailable(Exception):
    """Census could not be reached (do not cache as a no-match)."""


GEOCODE_NEGATIVE_TTL_S = 30 * 24 * 3600


class _GeocodeCache:
    """Append-only JSONL cache keyed by the one-line address query."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._rows: dict[str, tuple[dict[str, Any] | None, float]] = {}
        if path is not None and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("q"):
                    self._rows[str(rec["q"])] = (rec.get("r"), float(rec.get("t") or 0))

    def get(self, query: str) -> tuple[bool, dict[str, Any] | None]:
        with self._lock:
            found = self._rows.get(query)
        if found is None:
            return False, None
        result, stamp = found
        if result is None and time.time() - stamp > GEOCODE_NEGATIVE_TTL_S:
            return False, None
        return True, result

    def put(self, query: str, result: dict[str, Any] | None) -> None:
        stamp = time.time()
        with self._lock:
            self._rows[query] = (result, stamp)
            if self.path is None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"q": query, "r": result, "t": stamp}) + "\n")
            except OSError as exc:
                logger.info("geocode cache write skipped: %s", exc)


_cache_singleton: _GeocodeCache | None = None
_cache_singleton_lock = threading.Lock()


def _geocode_cache() -> _GeocodeCache:
    global _cache_singleton
    with _cache_singleton_lock:
        if _cache_singleton is None:
            path = None
            if env_flag("GEOCODE_CACHE", True):
                from paths import data_root

                path = data_root() / "cache" / "census_geocode.jsonl"
            _cache_singleton = _GeocodeCache(path)
        return _cache_singleton


# ------------------------------ batch geocode -------------------------------

CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
GEOCODE_BATCH_SIZE = max(1, min(10_000, env_int("GEOCODE_BATCH_SIZE", 1000)))
GEOCODE_BATCH_TIMEOUT_S = env_float("GEOCODE_BATCH_TIMEOUT_S", 300)


def parse_census_batch_csv(text: str) -> dict[str, dict[str, Any] | None]:
    """Census addressbatch CSV reply → {row id: geocode result or None}."""
    out: dict[str, dict[str, Any] | None] = {}
    for row in csv.reader(io.StringIO(text or "")):
        if not row or not row[0].strip():
            continue
        key = row[0].strip()
        status = row[2].strip().lower() if len(row) > 2 else ""
        result = None
        if status == "match" and len(row) > 5:
            try:
                lng_s, lat_s = row[5].split(",", 1)
                result = {
                    "lat": float(lat_s),
                    "lng": float(lng_s),
                    "matched": row[4].strip() if len(row) > 4 else "",
                    "source": "census",
                }
            except (ValueError, IndexError):
                result = None
        out[key] = result
    return out


def _fetch_census_batch(rows: list[tuple[str, str, str, str, str]]) -> dict[str, dict | None]:
    import requests

    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    resp = requests.post(
        CENSUS_BATCH_URL,
        data={"benchmark": "Public_AR_Current"},
        files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")},
        headers={"User-Agent": "site-orchestrator/enrichment"},
        timeout=GEOCODE_BATCH_TIMEOUT_S,
    )
    resp.raise_for_status()
    return parse_census_batch_csv(resp.text)


def geocode_sites(sites: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Geocode many Salesforce sites; {address query: result or None}.

    Cache first, then one Census batch request per GEOCODE_BATCH_SIZE
    addresses, then single-line lookups for anything a failed batch left.
    """
    if not geocode_address_enabled():
        return {}
    cache = _geocode_cache()
    results: dict[str, dict[str, Any] | None] = {}
    pending: dict[str, dict[str, Any]] = {}
    for site in sites:
        query = build_site_address(site)
        if not query or query in results or query in pending:
            continue
        hit, value = cache.get(query)
        if hit:
            results[query] = value
        else:
            pending[query] = site
    queries = list(pending)
    for start in range(0, len(queries), GEOCODE_BATCH_SIZE):
        chunk = queries[start : start + GEOCODE_BATCH_SIZE]
        rows = [
            (
                str(index),
                str(pending[q].get("Site_Street__c") or "").strip(),
                str(pending[q].get("Site_City__c") or "").strip(),
                str(pending[q].get("Site_State__c") or "").strip(),
                str(pending[q].get("Site_Zip_Code__c") or "").strip(),
            )
            for index, q in enumerate(chunk)
        ]
        try:
            batch = _fetch_census_batch(rows)
        except Exception as exc:  # noqa: BLE001 — fall back to one-line lookups
            logger.warning("Census batch geocode failed (%s); using single lookups", exc)
            for q in chunk:
                results[q] = geocode_census(q)
            continue
        for index, q in enumerate(chunk):
            value = batch.get(str(index))
            results[q] = value
            cache.put(q, value)
    return results


def pin_address_is_mismatch(offset_m: float | None) -> bool:
    if offset_m is None:
        return False
    return float(offset_m) >= float(PIN_ADDRESS_MISMATCH_M)


def should_compare_rooftop_hosts(
    offset_m: float | None, *, db_backed: bool = False
) -> bool:
    """No FCC/TowerSource hit: pin vs Census when they are not the same parcel.

    Towers are DB-anchored. Rooftops sit on a building; a parking-lot pin
    at ROOFTOP_HOST_OFFSET_M is enough to look at the street geocode.
    """
    if db_backed or offset_m is None:
        return False
    return float(offset_m) >= float(ROOFTOP_HOST_OFFSET_M)


def osm_anchor_score(osm: dict[str, Any] | None) -> int:
    """Rooftop-first OSM preference: host building matters; towers still win."""
    if not osm or not osm.get("ok"):
        return 0
    score = 0
    if osm.get("communication_tower"):
        score += 40
    if osm.get("has_tower_or_mast"):
        score += 25
    if osm.get("has_building"):
        score += 25
    return score


def naip_anchor_score(res: dict[str, Any] | None) -> int:
    """NAIP screen preference. Weak rooftop/tower labels do not win."""
    if not res:
        return 0
    site = str(res.get("site_type") or "").strip().lower()
    try:
        conf = float(res.get("site_confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if site == "tower" and conf >= 0.6:
        return 50
    if site == "rooftop" and conf >= 0.6:
        return 40
    if site == "tower":
        return 15
    if site == "rooftop":
        return 12
    return 0


def pick_classify_anchor(
    *,
    pin_osm: dict[str, Any] | None = None,
    address_osm: dict[str, Any] | None = None,
    pin_naip: dict[str, Any] | None = None,
    address_naip: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Choose SF pin vs Census address before buying Nearmap.

    Tie keeps the pin: Google rooftop-snap is often the building centroid,
    while Census interpolates the street. Callers should run this on the
    no-DB (rooftop) path only.
    """
    pin_s = osm_anchor_score(pin_osm) + naip_anchor_score(pin_naip)
    addr_s = osm_anchor_score(address_osm) + naip_anchor_score(address_naip)
    if addr_s > pin_s:
        return "address", f"address score {addr_s} > pin {pin_s}"
    if pin_s > addr_s:
        return "pin", f"pin score {pin_s} > address {addr_s}"
    return "pin", "tie — keep SF pin"


def mismatch_osm_is_decisive(
    pin_osm: dict[str, Any] | None,
    address_osm: dict[str, Any] | None,
) -> bool:
    """True when OSM already disagrees, so skip a dual NAIP screen."""
    return osm_anchor_score(pin_osm) != osm_anchor_score(address_osm)
