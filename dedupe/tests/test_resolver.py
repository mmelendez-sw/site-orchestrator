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


def test_score_candidate_strong_address_ignores_proximity_penalty():
    incoming = "1020 W HISTORIC MITCHELL ST, MILWAUKEE, WI 53204"
    sf_record = {
        "Id": "001",
        "Site_Address__c": "1020 West Historic Mitchell Street, Milwaukee, WI 53204",
        "Site_Latitude__c": 43.0113,
        "Site_Longitude__c": -87.9235,
    }
    scored = SiteResolver._score_candidate(
        incoming,
        43.012345,
        -87.924450,
        sf_record,
        search_radius_m=150,
    )
    assert scored["within_radius"] is True
    assert scored["address_score"] == 100
    assert scored["combined_score"] == 100


def test_prefilter_excludes_far_candidates():
    pool = [
        {
            "Id": "near",
            "Site_Address__c": "100 Main St",
            "Site_Latitude__c": 43.0526,
            "Site_Longitude__c": -87.9112,
        },
        {
            "Id": "far",
            "Site_Address__c": "999 Remote Rd",
            "Site_Latitude__c": 44.0,
            "Site_Longitude__c": -88.5,
        },
    ]
    filtered = SiteResolver._prefilter_candidates(
        pool,
        incoming_lat=43.052581,
        incoming_lng=-87.911206,
        incoming_zip="53212",
        max_distance_m=500,
    )
    assert len(filtered) == 1
    assert filtered[0]["Id"] == "near"


def test_resolve_match_status_strong_address_duplicate_within_radius():
    match = {
        "within_radius": True,
        "combined_score": 73,
        "address_score": 100,
        "proximity_score": 22,
        "distance_m": 117.0,
    }
    status, score, rule, zip_mismatch = SiteResolver._resolve_match_status(
        match,
        search_radius_m=150,
        incoming_zip="53204",
        matched_zip="53204",
        incoming_address="1020 W HISTORIC MITCHELL ST, MILWAUKEE, WI 53204",
        candidate_address="1020 West Historic Mitchell Street, Milwaukee, WI 53204",
    )
    assert status == "duplicate"
    assert score == 100
    assert rule == "high_address_exact"
    assert zip_mismatch is False


def test_resolve_match_status_geocoder_collision():
    match = {
        "within_radius": True,
        "combined_score": 60,
        "address_score": 45,
        "proximity_score": 89,
        "distance_m": 17.0,
    }
    status, score, rule, _ = SiteResolver._resolve_match_status(
        match,
        search_radius_m=150,
        incoming_zip="53202",
        matched_zip="53202",
        incoming_address="1888 N WATER ST, MILWAUKEE, WI 53202",
        candidate_address="1810 North Water Street, Milwaukee, WI 53202",
    )
    assert status == "review"
    assert rule == "geocoder_collision"


def test_resolve_match_status_high_address_far_goes_review():
    match = {
        "within_radius": False,
        "combined_score": 95,
        "address_score": 95,
        "proximity_score": 0,
        "distance_m": 179.0,
    }
    status, _, rule, _ = SiteResolver._resolve_match_status(
        match,
        search_radius_m=100,
        incoming_zip="53224",
        matched_zip="53224",
        incoming_address="10136 W FOND DU LAC AVE, MILWAUKEE, WI 53224",
        candidate_address="10136 W Fond Du Lac Ave, Milwaukee, WI 53224",
    )
    assert status == "review"
    assert rule == "high_address_far"


def test_resolve_match_status_address_floor_proximity_neighbors_net_new():
    match = {
        "within_radius": True,
        "combined_score": 61,
        "address_score": 45,
        "proximity_score": 90,
        "distance_m": 30.0,
    }
    status, _, rule, _ = SiteResolver._resolve_match_status(
        match,
        search_radius_m=100,
        incoming_zip="53215",
        matched_zip="53215",
        incoming_address="3530 W PIERCE ST, MILWAUKEE, WI 53215",
        candidate_address="3522 West Pierce Street, Milwaukee, WI 53215",
    )
    assert status == "net_new"
    assert rule == "house_number_neighbor"


def test_resolve_match_status_letter_suffix_duplicate():
    match = {
        "within_radius": True,
        "combined_score": 100,
        "address_score": 100,
        "proximity_score": 68,
        "distance_m": 32.0,
    }
    status, score, rule, _ = SiteResolver._resolve_match_status(
        match,
        search_radius_m=100,
        incoming_zip="53216",
        matched_zip="53216",
        incoming_address="4222 W CAPITOL DR, MILWAUKEE, WI 53216",
        candidate_address="4222A West Capitol Drive, Milwaukee, WI 53216",
    )
    assert status == "duplicate"
    assert score == 100
    assert rule == "high_address_exact"


def test_is_potential_duplicate_flags_borderline_band():
    match = {
        "within_radius": True,
        "combined_score": 40,
        "distance_m": 30.0,
    }
    assert SiteResolver.is_potential_duplicate(status="net_new", match=match) is True


def test_is_potential_duplicate_below_borderline():
    match = {
        "within_radius": True,
        "combined_score": 25,
        "distance_m": 30.0,
    }
    assert SiteResolver.is_potential_duplicate(status="net_new", match=match) is False


def test_score_candidate_nulls_proximity_for_geocoded_sf_fallback():
    incoming = "1800 W BECHER ST, MILWAUKEE, WI 53215"
    sf_record = {
        "Id": "001",
        "Site_Address__c": "1800 West Becher Street, Milwaukee, WI 53215",
        "_dedupe_geocoded_lat": 43.0113,
        "_dedupe_geocoded_lng": -87.9235,
    }
    scored = SiteResolver._score_candidate(
        incoming,
        43.0113,
        -87.9235,
        sf_record,
        search_radius_m=100,
    )
    assert scored["coordinate_source"] == "geocoded"
    assert scored["proximity_score"] is None
    assert scored["distance_m"] is None
    assert scored["combined_score"] == scored["address_score"]
