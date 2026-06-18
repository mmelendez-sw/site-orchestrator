"""Batch-level dedupe reconciliation after per-row Salesforce resolution."""

from __future__ import annotations

from typing import Any

from dedupe.address_match import (
    canonicalize_street_tokens,
    extract_street_line,
    house_number_delta,
    strip_house_number,
)
from dedupe.constants import (
    INPUT_DEDUPE_COORD_PRECISION,
    INPUT_NEAR_DEDUPE_HOUSE_MAX_DELTA,
    INPUT_NEAR_DEDUPE_MAX_M,
    THRESHOLD_VERSION,
)
from dedupe.urbanicity import urbanicity_for_record
from dedupe.match_snapshot import (
    apply_candidate_snapshot,
    clear_match_fields,
)
from dedupe.spatial import haversine_meters


def input_dedupe_key(record: dict[str, Any]) -> tuple[str, float, float]:
    street = canonicalize_street_tokens(extract_street_line(record.get("address")))
    lat = round(float(record["lat"]), INPUT_DEDUPE_COORD_PRECISION)
    lng = round(float(record["lng"]), INPUT_DEDUPE_COORD_PRECISION)
    return street, lat, lng


def _street_stems_match(left_address: str | None, right_address: str | None) -> bool:
    left = strip_house_number(extract_street_line(left_address or ""))
    right = strip_house_number(extract_street_line(right_address or ""))
    return bool(left and right and left == right)


def prescore_duplicate_indices(records: list[dict[str, Any]]) -> set[int]:
    """Return indices of input rows that duplicate an earlier row (pre-SF resolve)."""
    primary_index: dict[tuple[str, float, float], int] = {}
    duplicates: set[int] = set()
    for index, record in enumerate(records):
        key = input_dedupe_key(record)
        if key[0] == "":
            continue
        if key not in primary_index:
            primary_index[key] = index
            continue
        duplicates.add(index)
    return duplicates


def build_prescore_duplicate_row(
    canonical: dict[str, Any],
    *,
    reason: str = "duplicate_of_input_prescore",
) -> dict[str, Any]:
    """Build a result row for a pre-score input duplicate without SF matching."""
    urbanicity = urbanicity_for_record(canonical)
    urbanicity_data = urbanicity.as_dict()
    pop_text = (
        f"{urbanicity.population:,}"
        if urbanicity.population is not None
        else "unknown"
    )
    return {
        "address": canonical.get("address"),
        "lat": canonical.get("lat"),
        "lng": canonical.get("lng"),
        "zip_code": canonical.get("zip_code"),
        "urbanicity_tier": urbanicity_data.get("urbanicity_tier"),
        "zip_population": urbanicity_data.get("zip_population"),
        "urbanicity_prefilter_radius_m": urbanicity_data.get("search_radius_m"),
        "status": "duplicate",
        "status_resolver": "duplicate",
        "status_recommended": "duplicate",
        "address_score": 100,
        "proximity_score": 100,
        "combined_score": 100,
        "matched_distance_m": 0.0,
        "matched_coordinate_source": "input_dedupe",
        "spatial_candidate_count": 0,
        "prefilter_candidate_count": 0,
        "potential_duplicate": False,
        "candidate_count": 0,
        "matched_id": None,
        "matched_address": None,
        "matched_city": None,
        "matched_state": None,
        "matched_zip": None,
        "house_number_delta": 0,
        "suffix_mismatch": False,
        "city_mismatch": False,
        "runner_up_id": None,
        "runner_up_score": None,
        "tie_breaker_close": False,
        "routing_reason": reason,
        "proximity_rule": reason,
        "override_reason": reason,
        "status_source": "input_dedupe_prescore",
        "zip_mismatch": False,
        "scoring_mode": "",
        "top_candidates": "",
        "_gated_candidates": [],
        "threshold_version": THRESHOLD_VERSION,
        "distance_override_applied": False,
        "resolution_detail": (
            f"{urbanicity.tier} zip population={pop_text} "
            f"radius={int(urbanicity.search_radius_m)}m; "
            f"routing_reason={reason}; status=duplicate; "
            f"threshold_version={THRESHOLD_VERSION}"
        ),
    }


def mark_input_duplicates(rows: list[dict[str, Any]]) -> int:
    """Mark later rows that duplicate an earlier input row (same street + coords)."""
    primary_index: dict[tuple[str, float, float], int] = {}
    changed = 0

    for index, row in enumerate(rows):
        key = input_dedupe_key(row)
        if key[0] == "":
            continue
        if key not in primary_index:
            primary_index[key] = index
            continue
        if row.get("status_recommended") == "duplicate":
            continue

        _mark_input_duplicate(row, reason="duplicate_of_input")
        changed += 1

    return changed


def mark_input_duplicates_near(rows: list[dict[str, Any]]) -> int:
    """Cluster nearby input rows with matching street stems and close house numbers."""
    changed = 0
    claimed: set[int] = set()

    for primary_index, primary in enumerate(rows):
        if primary_index in claimed:
            continue
        if primary.get("status_recommended") == "duplicate":
            claimed.add(primary_index)
            continue

        for index in range(primary_index + 1, len(rows)):
            if index in claimed:
                continue
            row = rows[index]
            if row.get("status_recommended") == "duplicate":
                claimed.add(index)
                continue

            distance = haversine_meters(
                float(primary["lat"]),
                float(primary["lng"]),
                float(row["lat"]),
                float(row["lng"]),
            )
            if distance > INPUT_NEAR_DEDUPE_MAX_M:
                continue
            if not _street_stems_match(primary.get("address"), row.get("address")):
                continue

            delta = house_number_delta(primary.get("address", ""), row.get("address", ""))
            if delta is not None and delta > INPUT_NEAR_DEDUPE_HOUSE_MAX_DELTA:
                continue

            _mark_input_duplicate(row, reason="duplicate_of_input_near")
            claimed.add(index)
            changed += 1

    return changed


def _mark_input_duplicate(row: dict[str, Any], *, reason: str) -> None:
    row["status_recommended"] = "duplicate"
    row["status"] = "duplicate"
    row["routing_reason"] = reason
    row["proximity_rule"] = reason
    row["override_reason"] = reason
    row["status_source"] = "batch_input_dedupe"
    row["potential_duplicate"] = False
    row["resolution_detail"] = (
        f"{row.get('resolution_detail', '')}; batch={reason}"
    ).strip("; ")


def _next_candidate_snapshot(
    row: dict[str, Any],
    *,
    excluded_ids: set[str],
) -> dict[str, Any] | None:
    for snapshot in row.get("_gated_candidates") or []:
        matched_id = snapshot.get("matched_id")
        if not matched_id or matched_id in excluded_ids:
            continue
        return snapshot
    return None


def reconcile_shared_matched_ids(rows: list[dict[str, Any]]) -> int:
    """When multiple input rows match the same SF Id, only the best score wins."""
    by_matched_id: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        matched_id = row.get("matched_id")
        if not matched_id or row.get("routing_reason") in {
            "duplicate_of_input",
            "duplicate_of_input_near",
        }:
            continue
        by_matched_id.setdefault(str(matched_id), []).append(index)

    changed = 0
    claimed_ids: set[str] = set()

    for indices in by_matched_id.values():
        if len(indices) < 2:
            continue
        winner = max(
            indices,
            key=lambda idx: (
                rows[idx].get("combined_score") or 0,
                rows[idx].get("address_score") or 0,
                -(rows[idx].get("matched_distance_m") or float("inf")),
            ),
        )
        winner_id = str(rows[winner].get("matched_id"))
        claimed_ids.add(winner_id)

        for index in indices:
            if index == winner:
                continue
            row = rows[index]
            if row.get("status_recommended") == "duplicate" and row.get("matched_id") != winner_id:
                pass
            clear_match_fields(row)
            snapshot = _next_candidate_snapshot(row, excluded_ids=claimed_ids)
            radius = float(row.get("urbanicity_prefilter_radius_m") or 100)
            if snapshot:
                apply_candidate_snapshot(row, snapshot, search_radius_m=radius)
                row["status"] = row["status_recommended"]
                row["status_source"] = "batch_matched_id_reconcile"
                row["routing_reason"] = "matched_id_rerouted"
                row["proximity_rule"] = row.get("routing_reason")
                row["override_reason"] = None
                row["potential_duplicate"] = False
                new_id = snapshot.get("matched_id")
                if new_id and row.get("status_recommended") == "duplicate":
                    claimed_ids.add(str(new_id))
            else:
                row["status_recommended"] = "net_new"
                row["status"] = "net_new"
                row["routing_reason"] = "matched_id_already_claimed"
                row["proximity_rule"] = "matched_id_already_claimed"
                row["override_reason"] = None
                row["status_source"] = "batch_matched_id_reconcile"
                row["potential_duplicate"] = False

            row["resolution_detail"] = (
                f"{row.get('resolution_detail', '')}; batch=matched_id_reconcile"
            ).strip("; ")
            changed += 1

    return changed


def escalate_address_exact_distance_outliers(rows: list[dict[str, Any]]) -> int:
    """Escalate high-confidence address matches beyond 2x urbanicity radius."""
    changed = 0
    for row in rows:
        routing = row.get("routing_reason")
        if routing not in {"high_address_exact", "address_exact_distance_outlier"}:
            continue
        radius = float(row.get("urbanicity_prefilter_radius_m") or 100)
        distance = row.get("matched_distance_m")
        if distance is None or distance <= 2 * radius:
            continue
        if row.get("status_recommended") == "review":
            continue
        row["status_recommended"] = "review"
        row["status"] = "review"
        row["routing_reason"] = "address_exact_distance_outlier"
        row["proximity_rule"] = "address_exact_distance_outlier"
        row["override_reason"] = "address_exact_distance_outlier"
        row["resolution_detail"] = (
            f"{row.get('resolution_detail', '')}; batch=address_exact_distance_outlier"
        ).strip("; ")
        changed += 1
    return changed


def promote_potential_duplicates(rows: list[dict[str, Any]]) -> int:
    """potential_duplicate must not coexist with net_new status."""
    protected_routing = {
        "house_number_neighbor",
        "house_number_far",
        "intersection",
    }
    changed = 0
    for row in rows:
        if not row.get("potential_duplicate"):
            continue
        if row.get("routing_reason") in protected_routing:
            row["potential_duplicate"] = False
            continue
        if row.get("status_recommended") != "net_new":
            row["potential_duplicate"] = False
            continue
        row["status_recommended"] = "review"
        row["status"] = "review"
        row["routing_reason"] = "potential_duplicate_promoted"
        row["proximity_rule"] = "potential_duplicate_promoted"
        row["override_reason"] = None
        row["status_source"] = "batch_potential_duplicate"
        row["potential_duplicate"] = False
        row["resolution_detail"] = (
            f"{row.get('resolution_detail', '')}; batch=potential_duplicate_promoted"
        ).strip("; ")
        changed += 1
    return changed


def finalize_status_fields(rows: list[dict[str, Any]]) -> None:
    """Ensure exported status reflects the post-process recommendation."""
    for row in rows:
        recommended = row.get("status_recommended") or row.get("status_resolver") or row.get("status")
        row["status_recommended"] = recommended
        row["status"] = recommended
        if row.get("status_resolver") is None:
            row["status_resolver"] = recommended


def apply_batch_postprocess(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Run all batch reconciliation passes and return change counts."""
    counts = {
        "input_duplicates": mark_input_duplicates(rows),
        "input_duplicates_near": mark_input_duplicates_near(rows),
        "matched_id_reconciled": reconcile_shared_matched_ids(rows),
        "address_exact_outliers": escalate_address_exact_distance_outliers(rows),
        "potential_duplicate_promoted": promote_potential_duplicates(rows),
    }
    finalize_status_fields(rows)
    return counts
