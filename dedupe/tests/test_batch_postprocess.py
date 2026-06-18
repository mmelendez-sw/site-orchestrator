"""Tests for batch dedupe postprocessing."""

from dedupe.batch_postprocess import (
    apply_batch_postprocess,
    mark_input_duplicates,
    promote_potential_duplicates,
    reconcile_shared_matched_ids,
)


def _row(**kwargs):
    base = {
        "address": "100 Main St",
        "lat": 43.0,
        "lng": -87.9,
        "status": "net_new",
        "combined_score": 50,
        "address_score": 50,
        "matched_id": None,
        "potential_duplicate": False,
        "resolution_detail": "status=net_new",
    }
    base.update(kwargs)
    return base


def test_mark_input_duplicates():
    rows = [
        _row(address="100 Main St, Milwaukee, WI 53212"),
        _row(address="100 MAIN ST, MILWAUKEE, WI 53212"),
    ]
    changed = mark_input_duplicates(rows)
    assert changed == 1
    assert rows[0]["status"] == "net_new"
    assert rows[1]["status"] == "duplicate"
    assert rows[1]["override_reason"] == "duplicate_of_input"


def test_reconcile_shared_matched_ids():
    rows = [
        _row(status="duplicate", matched_id="001", combined_score=100, address_score=100),
        _row(status="net_new", matched_id="001", combined_score=52, address_score=45),
    ]
    changed = reconcile_shared_matched_ids(rows)
    assert changed == 1
    assert rows[1]["status"] == "duplicate"
    assert rows[1]["override_reason"] == "matched_id_already_claimed"


def test_promote_potential_duplicates():
    rows = [_row(status="net_new", potential_duplicate=True)]
    changed = promote_potential_duplicates(rows)
    assert changed == 1
    assert rows[0]["status"] == "review"
    assert rows[0]["override_reason"] == "potential_duplicate_promoted"


def test_apply_batch_postprocess_runs_all_passes():
    rows = [
        _row(address="1 Main", lat=1.0, lng=2.0),
        _row(address="1 MAIN", lat=1.0, lng=2.0),
        _row(status="net_new", potential_duplicate=True, matched_id="abc"),
    ]
    summary = apply_batch_postprocess(rows)
    assert summary["input_duplicates"] == 1
    assert summary["potential_duplicate_promoted"] == 1
