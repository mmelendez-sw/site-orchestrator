"""Batch-level dedupe reconciliation after per-row Salesforce resolution."""

from __future__ import annotations

from typing import Any

from dedupe.address_match import canonicalize_street_tokens, extract_street_line
from dedupe.constants import INPUT_DEDUPE_COORD_PRECISION


def input_dedupe_key(record: dict[str, Any]) -> tuple[str, float, float]:
    street = canonicalize_street_tokens(extract_street_line(record.get("address")))
    lat = round(float(record["lat"]), INPUT_DEDUPE_COORD_PRECISION)
    lng = round(float(record["lng"]), INPUT_DEDUPE_COORD_PRECISION)
    return street, lat, lng


def mark_input_duplicates(rows: list[dict[str, Any]]) -> int:
    """R1: Mark later rows that duplicate an earlier input row (same street + coords)."""
    primary_index: dict[tuple[str, float, float], int] = {}
    changed = 0

    for index, row in enumerate(rows):
        key = input_dedupe_key(row)
        if key[0] == "":
            continue
        if key not in primary_index:
            primary_index[key] = index
            continue
        if row.get("status") == "duplicate":
            continue

        row["status"] = "duplicate"
        row["override_reason"] = "duplicate_of_input"
        row["status_source"] = "batch_input_dedupe"
        row["potential_duplicate"] = False
        row["resolution_detail"] = (
            f"{row.get('resolution_detail', '')}; batch=duplicate_of_input"
        ).strip("; ")
        changed += 1

    return changed


def reconcile_shared_matched_ids(rows: list[dict[str, Any]]) -> int:
    """R2: When multiple input rows match the same SF Id, only the best score wins."""
    by_matched_id: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        matched_id = row.get("matched_id")
        if not matched_id or row.get("override_reason") == "duplicate_of_input":
            continue
        by_matched_id.setdefault(str(matched_id), []).append(index)

    changed = 0
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
        for index in indices:
            if index == winner:
                continue
            row = rows[index]
            if row.get("status") == "duplicate":
                continue
            row["status"] = "duplicate"
            row["override_reason"] = "matched_id_already_claimed"
            row["status_source"] = "batch_matched_id_reconcile"
            row["potential_duplicate"] = False
            row["resolution_detail"] = (
                f"{row.get('resolution_detail', '')}; batch=matched_id_already_claimed"
            ).strip("; ")
            changed += 1

    return changed


def promote_potential_duplicates(rows: list[dict[str, Any]]) -> int:
    """R3: potential_duplicate must not coexist with net_new status."""
    changed = 0
    for row in rows:
        if not row.get("potential_duplicate"):
            continue
        if row.get("status") != "net_new":
            row["potential_duplicate"] = False
            continue
        row["status"] = "review"
        row["override_reason"] = "potential_duplicate_promoted"
        row["status_source"] = "batch_potential_duplicate"
        row["potential_duplicate"] = False
        row["resolution_detail"] = (
            f"{row.get('resolution_detail', '')}; batch=potential_duplicate_promoted"
        ).strip("; ")
        changed += 1
    return changed


def apply_batch_postprocess(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Run all batch reconciliation passes and return change counts."""
    return {
        "input_duplicates": mark_input_duplicates(rows),
        "matched_id_reconciled": reconcile_shared_matched_ids(rows),
        "potential_duplicate_promoted": promote_potential_duplicates(rows),
    }
