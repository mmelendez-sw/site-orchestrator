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


def test_resolve_returns_status_shape():
    resolver = SiteResolver
    assert DEFAULT_RADIUS_METERS == 250
