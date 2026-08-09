"""Tests for street-line address matching."""

from dedupe.address_match import (
    address_match_score,
    canonicalize_street_tokens,
    collapse_osm_address,
    evaluate_match_features,
    extract_house_number,
    extract_street_line,
    house_number_delta,
    house_numbers_equivalent,
    normalize_for_scoring,
    strip_leading_poi,
    street_token_jaccard,
    suffix_mismatch,
)
from ingest.address_utils import parse_zip_from_address


def test_extract_street_line_strips_city_state_zip():
    street = extract_street_line("1525 N 24TH ST, MILWAUKEE, WI 53205")
    assert street == "1525 N 24TH ST"


def test_extract_street_line_handles_salesforce_html():
    street = extract_street_line("1800 W BECHER ST<br>MILWAUKEE, WI 53215")
    assert street == "1800 W BECHER ST"


def test_canonicalize_street_tokens_expands_abbreviations():
    assert canonicalize_street_tokens("1525 N 24th St") == "1525 NORTH 24TH STREET"


def test_extract_house_number():
    assert extract_house_number("1525 North 24th Street") == "1525"
    assert extract_house_number("727-733 North Van Buren Street") == "727-733"
    assert extract_house_number("North Avenue") is None


def test_house_numbers_equivalent_for_range():
    assert house_numbers_equivalent("727 N VAN BUREN ST", "727-733 N VAN BUREN ST") is True
    assert house_numbers_equivalent("1525 N 24TH ST", "1520 N 24TH ST") is False


def test_house_numbers_equivalent_for_letter_suffix():
    assert house_numbers_equivalent("4222 W CAPITOL DR", "4222A WEST CAPITOL DR") is True
    assert house_numbers_equivalent("4222A W CAPITOL DR", "4222B WEST CAPITOL DR") is False


def test_address_match_score_treats_letter_suffix_as_exact():
    score = address_match_score(
        "4222 W CAPITOL DR, MILWAUKEE, WI 53216",
        "4222A West Capitol Drive<br>Milwaukee, WI 53216",
    )
    assert score == 100


def test_house_number_neighbor_auto_net_new():
    from dedupe.address_match import house_number_neighbor_auto_net_new

    assert house_number_neighbor_auto_net_new(
        "2059 S 33RD ST, MILWAUKEE, WI 53215",
        "2053 South 33rd Street, Milwaukee, WI 53215",
    )
    assert house_number_neighbor_auto_net_new(
        "3530 W PIERCE ST, MILWAUKEE, WI 53215",
        "3522 West Pierce Street, Milwaukee, WI 53215",
    )
    assert not house_number_neighbor_auto_net_new(
        "4222 W CAPITOL DR, MILWAUKEE, WI 53216",
        "4222A West Capitol Drive, Milwaukee, WI 53216",
    )


def test_address_match_score_similar_streets():
    score = address_match_score(
        "1525 N 24TH ST, MILWAUKEE, WI 53205",
        "1525 North 24th Street<br>Milwaukee, WI 53205",
    )
    assert score >= 90


def test_address_match_score_treats_range_as_exact():
    score = address_match_score(
        "727 N VAN BUREN ST, MILWAUKEE, WI 53202",
        "727-733 North Van Buren Street<br>Milwaukee, WI 53202",
    )
    assert score == 100


def test_address_match_score_penalizes_different_house_numbers():
    score = address_match_score(
        "1525 N 24TH ST, MILWAUKEE, WI 53205",
        "1520 North 24th Street, Milwaukee, WI 53205",
    )
    assert score <= 45


def test_address_match_score_penalizes_geocoder_collision():
    score = address_match_score(
        "1888 N WATER ST, MILWAUKEE, WI 53202",
        "1810 North Water Street, Milwaukee, WI 53202",
    )
    assert score <= 45


def test_street_token_jaccard_detects_different_streets():
    score = street_token_jaccard(
        "10700 W BROWN DEER RD, MILWAUKEE, WI 53224",
        "8847 North 107th Street, Milwaukee, WI 53224",
    )
    assert score < 0.5


def test_parse_zip_from_verbose_address():
    address = (
        "West Carmen Avenue, Silverswan, Milwaukee, Milwaukee County, "
        "Wisconsin, 53225, United States"
    )
    assert parse_zip_from_address(address) == "53225"


def test_strip_leading_poi_fire_station():
    raw = (
        "Fire Station 10, 5600, West Oklahoma Avenue, White Manor, Milwaukee, "
        "Milwaukee County, Wisconsin, 53219, United States"
    )
    assert strip_leading_poi(raw).startswith("5600")


def test_strip_leading_poi_building_name():
    assert strip_leading_poi("Atrium Bldg,6815 W Capitol Dr") == "6815 W Capitol Dr"


def test_collapse_osm_address_to_usps_style():
    raw = (
        "5825, West Fairview Avenue, Story Hill, Milwaukee, Milwaukee County, "
        "Wisconsin, 53208, United States"
    )
    collapsed = collapse_osm_address(raw)
    assert "5825" in collapsed
    assert "MILWAUKEE" in collapsed
    assert "53208" in collapsed
    assert "WI" in collapsed
    assert "COUNTY" not in collapsed.upper()
    assert "STORY HILL" not in collapsed.upper()


def test_collapse_osm_address_dc_ward_sets_washington():
    raw = (
        "1201, 4th Street Southeast, Capitol Waterfront Neighborhood, "
        "Ward 8, Washington, District of Columbia, 20003, United States"
    )
    collapsed = collapse_osm_address(raw)
    assert "1201" in collapsed
    assert "WASHINGTON" in collapsed
    assert "DC" in collapsed
    assert "20003" in collapsed
    assert "WARD" not in collapsed.upper()


def test_normalize_for_scoring_improves_fire_station_match():
    incoming = (
        "Fire Station 10, 5600, West Oklahoma Avenue, White Manor, Milwaukee, "
        "Milwaukee County, Wisconsin, 53219, United States"
    )
    candidate = "5600 W Oklahoma Ave<br>Milwaukee, WI 53219"
    before = address_match_score(incoming, candidate)
    normalized_incoming = normalize_for_scoring(incoming)
    assert "5600" in normalized_incoming
    assert before >= 65


def test_house_number_delta():
    assert house_number_delta("1888 N WATER ST", "1810 North Water Street") == 78
    assert house_number_delta("3530 W PIERCE ST", "3522 West Pierce Street") == 8


def test_suffix_mismatch_detects_ave_vs_dr():
    assert suffix_mismatch(
        "5825 W FAIRVIEW AVE, MILWAUKEE, WI 53208",
        "5825 W. Fairview Dr., Milwaukee, WI 53214",
    )


def test_evaluate_match_features_rejects_different_streets():
    features = evaluate_match_features(
        "10700 W BROWN DEER RD, MILWAUKEE, WI 53224",
        "8847 North 107th Street, Milwaukee, WI 53224",
        distance_m=18.5,
    )
    assert features["passed"] is False
    assert features["hard_gate_reason"] == "hard_gate_street_mismatch"


def test_evaluate_match_features_rejects_large_house_number_delta():
    features = evaluate_match_features(
        "1888 N WATER ST, MILWAUKEE, WI 53202",
        "1810 North Water Street, Milwaukee, WI 53202",
        distance_m=16.5,
    )
    assert features["passed"] is False
    assert features["hard_gate_reason"] == "hard_gate_house_number_delta"


def test_evaluate_match_features_rejects_cross_city_at_distance():
    features = evaluate_match_features(
        "601 S 76TH ST, MILWAUKEE, WI 53214",
        "601 S 76Th St, WEST ALLIS, WI 53214",
        incoming_city="MILWAUKEE",
        matched_city="WEST ALLIS",
        distance_m=37.2,
    )
    assert features["city_mismatch"] is True
    assert features["passed"] is False
    assert features["hard_gate_reason"] == "hard_gate_city_mismatch"
