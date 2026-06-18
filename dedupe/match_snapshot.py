"""Serialize scored Salesforce candidates for batch re-resolution."""

from __future__ import annotations

import json
from typing import Any

from dedupe.address_match import normalize_sf_address
from dedupe.constants import (
    SF_ADDRESS_FIELD,
    SF_CITY_FIELD,
    SF_STATE_FIELD,
    SF_ZIP_FIELD,
)


def normalize_zip(value: Any) -> str | None:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) >= 5:
        return digits[:5]
    return None


def serialize_scored_candidate(
    item: dict[str, Any],
    *,
    routing_reason: str | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe snapshot of a scored Salesforce candidate."""
    record = item["record"]
    features = item.get("match_features") or {}
    return {
        "matched_id": record.get("Id"),
        "matched_address": normalize_sf_address(
            record.get(SF_ADDRESS_FIELD) or record.get("Name") or ""
        ),
        "matched_city": record.get(SF_CITY_FIELD),
        "matched_state": record.get(SF_STATE_FIELD),
        "matched_zip": record.get(SF_ZIP_FIELD),
        "address_score": item.get("address_score"),
        "proximity_score": item.get("proximity_score"),
        "combined_score": item.get("combined_score"),
        "distance_m": item.get("distance_m"),
        "coordinate_source": item.get("coordinate_source"),
        "scoring_mode": item.get("scoring_mode"),
        "routing_reason": routing_reason,
        "match_features": dict(features),
    }


def top_candidates_json(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return ""
    return json.dumps(candidates, separators=(",", ":"))


def clear_match_fields(row: dict[str, Any]) -> None:
    """Remove winner-specific match metadata from a row."""
    for key in (
        "matched_id",
        "matched_address",
        "matched_city",
        "matched_state",
        "matched_zip",
        "matched_distance_m",
        "matched_coordinate_source",
        "address_score",
        "proximity_score",
        "combined_score",
        "proximity_rule",
        "routing_reason",
        "house_number_delta",
        "suffix_mismatch",
        "city_mismatch",
        "runner_up_id",
        "runner_up_score",
        "tie_breaker_close",
        "scoring_mode",
        "zip_mismatch",
    ):
        row[key] = None
    row["suffix_mismatch"] = False
    row["city_mismatch"] = False
    row["tie_breaker_close"] = False
    row["zip_mismatch"] = False


def apply_candidate_snapshot(
    row: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    search_radius_m: float,
) -> str:
    """Apply a candidate snapshot to a result row and return status_recommended."""
    features = snapshot.get("match_features") or {}
    row["matched_id"] = snapshot.get("matched_id")
    row["matched_address"] = snapshot.get("matched_address")
    row["matched_city"] = snapshot.get("matched_city")
    row["matched_state"] = snapshot.get("matched_state")
    row["matched_zip"] = snapshot.get("matched_zip")
    row["matched_distance_m"] = snapshot.get("distance_m")
    row["matched_coordinate_source"] = snapshot.get("coordinate_source")
    row["address_score"] = snapshot.get("address_score")
    row["proximity_score"] = snapshot.get("proximity_score")
    row["combined_score"] = snapshot.get("combined_score")
    row["scoring_mode"] = snapshot.get("scoring_mode")
    row["routing_reason"] = snapshot.get("routing_reason")
    row["proximity_rule"] = snapshot.get("routing_reason")
    row["house_number_delta"] = features.get("house_number_delta")
    row["suffix_mismatch"] = bool(features.get("suffix_mismatch"))
    row["city_mismatch"] = bool(features.get("city_mismatch"))
    incoming_zip = normalize_zip(row.get("zip_code"))
    matched_zip = normalize_zip(snapshot.get("matched_zip"))
    row["zip_mismatch"] = bool(
        incoming_zip and matched_zip and incoming_zip != matched_zip
    )

    routing = snapshot.get("routing_reason")
    if routing == "high_address_exact":
        distance = snapshot.get("distance_m")
        if distance is not None and distance > 2 * search_radius_m:
            row["status_recommended"] = "review"
            row["routing_reason"] = "address_exact_distance_outlier"
            row["proximity_rule"] = "address_exact_distance_outlier"
            row["override_reason"] = "address_exact_distance_outlier"
            return "review"

    status = status_from_routing(routing, row.get("combined_score") or 0)
    row["status_recommended"] = status
    return status


def status_from_routing(routing_reason: str | None, combined_score: int) -> str:
    if routing_reason in {
        "high_address_exact",
        "high_address_match",
        "proximity_duplicate",
        "address_exact_distance_override",
    }:
        return "duplicate"
    if routing_reason in {
        "geocoder_collision",
        "geocoder_collision_suspect",
        "address_floor_proximity",
        "zip_mismatch_low_distance",
        "high_address_far",
        "proximity_review",
        "proximity_review_extended",
        "city_mismatch_high_confidence",
        "address_exact_distance_outlier",
        "tie_breaker_close",
    }:
        return "review"
    if routing_reason in {
        "house_number_neighbor",
        "house_number_far",
        "intersection",
    }:
        return "net_new"
    if combined_score >= 85:
        return "duplicate"
    if combined_score >= 60:
        return "review"
    return "net_new"
