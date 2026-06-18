"""Tests for street-line address matching."""

from dedupe.address_match import (
    address_match_score,
    canonicalize_street_tokens,
    extract_house_number,
    extract_street_line,
    house_numbers_equivalent,
    street_token_jaccard,
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
