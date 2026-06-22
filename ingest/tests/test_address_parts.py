"""Tests for address component parsing."""

from ingest.address_parts import parse_address_components


def test_parse_address_components_standard_us_address():
    parts = parse_address_components("44 S Broadway, White Plains, NY 10601")
    assert parts["site_street"] == "44 S Broadway"
    assert parts["site_city"] == "WHITE PLAINS"
    assert parts["site_state"] == "NY"
    assert parts["zip_code"] == "10601"
    assert parts["site_country"] == "US"


def test_parse_address_components_dc():
    parts = parse_address_components(
        "1011 N CAPITOL ST NE, WASHINGTON, DC, 20002",
        zip_code="20002",
    )
    assert parts["site_street"] == "1011 N CAPITOL ST NE"
    assert parts["site_state"] == "DC"
    assert parts["zip_code"] == "20002"
