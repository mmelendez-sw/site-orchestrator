"""Tests for Salesforce upload template mapping."""

from datetime import datetime

from salesforce.upload_template import (
    build_upload_record,
    map_classifier_site_type,
    permit_scraping_carrier_leasing_source,
    upload_record_to_csv_row,
    validate_upload_record,
)


def test_permit_scraping_carrier_leasing_source():
    assert permit_scraping_carrier_leasing_source(datetime(2026, 6, 18)) == "PermitScraping_jun2026"
    assert permit_scraping_carrier_leasing_source(datetime(2026, 1, 5)) == "PermitScraping_jan2026"


def test_upload_record_matches_template_columns():
    record = build_upload_record(
        {
            "address": "44 S Broadway, White Plains, NY 10601",
            "lat": 41.03062,
            "lng": -73.7617,
            "zip_code": "10601",
            "permit_metadata": {"permit_id": "123"},
        },
        classified={"site_type": "rooftop", "site_confidence": 0.9},
        dedupe_row={"urbanicity_tier": "suburban", "zip_population": 10000},
        carrier_leasing_source="PermitScraping_jun2026",
    )
    row = upload_record_to_csv_row(record)
    assert row["Site Street"] == "44 S Broadway"
    assert row["Site City"] == "WHITE PLAINS"
    assert row["Site State"] == "NY"
    assert row["Site Zip Code"] == "10601"
    assert row["Site Country"] == "US"
    assert row["Site Latitude"] == "41.03062"
    assert row["Site Longitude"] == "-73.76170"
    assert row["Carrier Leasing Source"] == "PermitScraping_jun2026"
    assert row["Site Type"] == "Rooftop"
    assert row["Verified Site"] == "TRUE"
    assert row["Verified Site Source"] == "Permitting Data"
    assert row["Morphology"] == "Suburban"
    assert validate_upload_record(record) == []


def test_map_classifier_site_type():
    assert map_classifier_site_type("tower", tower_subtype="monopole") == "Monopole"
    assert map_classifier_site_type("rooftop") == "Rooftop"
    assert map_classifier_site_type("unclear") == ""
