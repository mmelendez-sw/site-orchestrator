"""Tests for end-of-run orchestrator summary."""

from orchestrator_summary import build_run_summary, format_run_summary


def test_format_run_summary_includes_tiers_and_sf_outcomes():
    summary = build_run_summary(
        processed=5,
        geocode_ok=4,
        geocode_failed=1,
        result_rows=[
            {"status": "duplicate", "address": "1 Dup St", "matched_id": "a01"},
            {"status": "net_new", "address": "2 New Ave"},
            {"status": "net_new", "address": "3 Tower Rd"},
            {"status": "review", "address": "4 Review Ln"},
        ],
        classified_by_index={
            1: {"nearmap_tier": "naip_only", "site_type": "rooftop"},
            2: {"nearmap_tier": "full", "site_type": "tower"},
        },
        upload_outcomes=[
            {"address": "2 New Ave", "status": "loaded", "sf_id": "site1"},
            {"address": "3 Tower Rd", "status": "failed", "error": "Missing required field: Site State"},
        ],
    )
    text = format_run_summary(summary)
    assert "duplicates : 1" in text
    assert "1 Dup St" in text
    assert "NAIP only: 1  (tower=0, rooftop=1, other=0)" in text
    assert "Nearmap obliques: 1  (tower=1, rooftop=0, other=0)" in text
    assert "loaded    : 1" in text
    assert "failed    : 1" in text
    assert "Site State" in text
