"""Tests for dedupe dataset context."""

from dedupe.context import build_dataset_bounding_box, build_dataset_context, extract_zip_codes


def test_extract_zip_codes_from_records():
    records = [
        {"address": "100 Main St, Milwaukee, WI 53202", "lat": 43.0, "lng": -87.9},
        {"address": "200 Oak Ave, Milwaukee, WI 53203", "lat": 43.01, "lng": -87.91},
    ]
    assert extract_zip_codes(records) == ["53202", "53203"]


def test_extract_zip_ignores_street_number_prefix():
    records = [
        {
            "address": "10001 W BLUE MOUND RD, MILWAUKEE, WI 53226",
            "lat": 43.0,
            "lng": -87.9,
            "zip_code": "53226",
        },
    ]
    assert extract_zip_codes(records) == ["53226"]


def test_build_dataset_bounding_box_expands_by_buffer():
    records = [
        {"lat": 43.0, "lng": -88.0},
        {"lat": 43.1, "lng": -87.9},
    ]
    bbox = build_dataset_bounding_box(records, meters=250)
    assert bbox["min_lat"] < 43.0
    assert bbox["max_lat"] > 43.1
    assert bbox["min_lng"] < -88.0
    assert bbox["max_lng"] > -87.9


def test_build_dataset_context_includes_zip_and_bbox():
    records = [
        {"address": "100 Main St, WI 53202", "lat": 43.0, "lng": -87.9},
    ]
    context = build_dataset_context(records)
    assert context["zip_codes"] == ["53202"]
    assert context["bbox"] is not None
    assert context["record_count"] == 1
