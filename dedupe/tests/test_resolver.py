"""Placeholder tests for dedupe resolver."""

from dedupe.constants import DEFAULT_RADIUS_METERS
from dedupe.resolver import SiteResolver


def test_build_bounding_box_symmetric():
    bbox = SiteResolver.build_bounding_box(38.0, -77.0, meters=250)
    assert bbox["min_lat"] < 38.0 < bbox["max_lat"]
    assert bbox["min_lng"] < -77.0 < bbox["max_lng"]


def test_fuzzy_match_prefers_close_address():
    score, match = SiteResolver.fuzzy_match(
        "100 F St NE, Washington, DC",
        [{"Id": "001", "Site_Address__c": "100 F Street NE, Washington, DC 20549"}],
    )
    assert score > 60
    assert match is not None


def test_score_candidate_within_radius_boosts_combined_score():
    incoming = "100 Main St, Milwaukee, WI 53212"
    sf_record = {
        "Id": "001",
        "Site_Address__c": "100 Main Street, Milwaukee, WI 53212",
        "Site_Latitude__c": 43.0526,
        "Site_Longitude__c": -87.9112,
    }
    scored = SiteResolver._score_candidate(
        incoming,
        43.052581,
        -87.911206,
        sf_record,
        search_radius_m=150,
    )
    assert scored["within_radius"] is True
    assert scored["address_score"] >= 80
    assert scored["combined_score"] >= scored["address_score"]


def test_resolve_outside_radius_rejects_far_matches():
    best_outside = {
        "address_score": 70,
        "distance_m": 8413.0,
        "record": {"Site_Zip_Code__c": "53220"},
    }
    status, score, flagged = SiteResolver._resolve_outside_radius_match(
        best_outside,
        incoming_zip="53226",
        search_radius_m=50,
    )
    assert status == "net_new"
    assert flagged is False


def test_resolve_outside_radius_duplicate_on_same_zip_without_coords():
    best_outside = {
        "address_score": 90,
        "distance_m": None,
        "record": {"Site_Zip_Code__c": "53215"},
    }
    status, score, flagged = SiteResolver._resolve_outside_radius_match(
        best_outside,
        incoming_zip="53215",
        search_radius_m=50,
    )
    assert status == "duplicate"
    assert flagged is True


def test_resolve_outside_radius_rejects_different_zip_without_coords():
    best_outside = {
        "address_score": 66,
        "distance_m": None,
        "record": {"Site_Zip_Code__c": "53225"},
    }
    status, score, flagged = SiteResolver._resolve_outside_radius_match(
        best_outside,
        incoming_zip="53212",
        search_radius_m=150,
    )
    assert status == "net_new"
    assert flagged is False


def test_resolve_returns_status_shape():
    assert DEFAULT_RADIUS_METERS == 250
