"""Tests for Salesforce upload template mapping."""

from datetime import datetime

from salesforce.upload_template import (
    DEFAULT_OWNER_ID,
    build_upload_record,
    map_classifier_site_type,
    permit_scraping_carrier_leasing_source,
    upload_record_to_csv_row,
    validate_upload_record,
)


def test_permit_scraping_carrier_leasing_source():
    assert permit_scraping_carrier_leasing_source(datetime(2026, 6, 18)) == "JF_PermitScraping_jun26"
    assert permit_scraping_carrier_leasing_source(datetime(2026, 1, 5)) == "JF_PermitScraping_jan26"
    assert permit_scraping_carrier_leasing_source(datetime(2026, 7, 22)) == "JF_PermitScraping_jul26"


def test_upload_record_matches_template_columns():
    record = build_upload_record(
        {
            "address": "44 S Broadway, White Plains, NY 10601",
            "lat": 41.03062,
            "lng": -73.7617,
            "zip_code": "10601",
            "permit_metadata": {"permit_id": "123"},
        },
        classified={"site_type": "rooftop", "site_confidence": 0.9, "cell_equipment": True},
        dedupe_row={"urbanicity_tier": "suburban", "zip_population": 10000},
        carrier_leasing_source="JF_PermitScraping_jul26",
    )
    row = upload_record_to_csv_row(record)
    assert row["Site Street"] == "44 S Broadway"
    assert row["Site City"] == "White Plains"
    assert row["Site State"] == "NY"
    assert row["Site Zip Code"] == "10601"
    assert row["Site Country"] == "US"
    assert row["Site Latitude"] == "41.03062"
    assert row["Site Longitude"] == "-73.76170"
    assert row["Carrier Leasing Source"] == "JF_PermitScraping_jul26"
    assert row["OwnerId"] == DEFAULT_OWNER_ID
    assert row["Site Type"] == "Rooftop"
    assert row["Verified Site"] == "TRUE"
    assert row["Verified Site Source"] == "Permitting Data"
    assert row["Morphology"] == "Suburban"
    assert validate_upload_record(record) == []


def test_map_classifier_site_type():
    assert map_classifier_site_type("tower", tower_subtype="monopole") == "Monopole"
    assert map_classifier_site_type("rooftop", cell_equipment=True) == "Rooftop"
    assert map_classifier_site_type("rooftop", cell_equipment=False) == ""
    assert map_classifier_site_type("unclear") == ""


def test_csv_row_roundtrip():
    from salesforce.upload_template import csv_row_to_upload_record

    record = build_upload_record(
        {
            "address": "44 S Broadway, White Plains, NY 10601",
            "lat": 41.03062,
            "lng": -73.7617,
            "zip_code": "10601",
        },
        classified={"site_type": "rooftop", "cell_equipment": True},
        dedupe_row={"urbanicity_tier": "suburban", "zip_population": 10000},
        carrier_leasing_source="JF_PermitScraping_jul26",
    )
    row = upload_record_to_csv_row(record)
    restored = csv_row_to_upload_record(row)
    assert restored["site_street"] == "44 S Broadway"
    assert restored["site_type"] == "Rooftop"
    assert restored["carrier_leasing_source"] == "JF_PermitScraping_jul26"
    assert restored["owner_id"] == DEFAULT_OWNER_ID
    assert validate_upload_record(restored) == []


def test_verified_site_source_defaults_to_permitting_data_without_metadata():
    record = build_upload_record(
        {
            "address": "100 E PLEASANT ST, MILWAUKEE, WI 53212",
            "lat": 43.05,
            "lng": -87.91,
        },
        classified={"site_type": "rooftop", "cell_equipment": True},
    )
    assert record["verified_site_source"] == "Permitting Data"
    row = upload_record_to_csv_row(record)
    assert row["Verified Site Source"] == "Permitting Data"
    assert row["Site Street"] == "100 E Pleasant St"
    assert row["Site City"] == "Milwaukee"
    assert row["Site State"] == "WI"
    assert row["Site Type"] == "Rooftop"
    assert row["OwnerId"] == DEFAULT_OWNER_ID


def test_rooftop_without_cell_equipment_omits_site_type_but_still_builds():
    record = build_upload_record(
        {
            "address": "100 E PLEASANT ST, MILWAUKEE, WI 53212",
            "lat": 43.05,
            "lng": -87.91,
        },
        classified={"site_type": "rooftop", "cell_equipment": False},
        carrier_leasing_source="JF_PermitScraping_jul26",
    )
    assert record["site_type"] == ""
    row = upload_record_to_csv_row(record)
    assert row["Site Type"] == ""
    assert row["Site Street"] == "100 E Pleasant St"
    assert validate_upload_record(record) == []


def test_build_upload_record_parses_census_state_comma_format():
    record = build_upload_record(
        {
            "address": "1361 W NORTH AVE, MILWAUKEE, WI, 53205",
            "lat": 43.06,
            "lng": -87.93,
            "zip_code": "53205",
        },
        classified={"site_type": "rooftop"},
        carrier_leasing_source="JF_PermitScraping_jul26",
    )
    assert record["site_state"] == "WI"
    assert validate_upload_record(record) == []


def test_build_upload_record_falls_back_to_scope_state():
    record = build_upload_record(
        {
            "address": "Unknown format without parseable state",
            "lat": 43.06,
            "lng": -87.93,
            "zip_code": "53205",
            "permit_metadata": {"scope_state": "WI"},
        },
        classified={"site_type": "rooftop"},
        carrier_leasing_source="JF_PermitScraping_jul26",
    )
    assert record["site_state"] == "WI"


def test_build_upload_record_parses_nominatim_full_state_name():
    record = build_upload_record(
        {
            "address": (
                "West Carmen Avenue, Silverswan, Milwaukee, "
                "Milwaukee County, Wisconsin, 53225, United States"
            ),
            "lat": 43.12,
            "lng": -88.03,
            "zip_code": "53225",
            "state": "WI",
        },
        classified={"site_type": "rooftop"},
        carrier_leasing_source="JF_PermitScraping_jul26",
    )
    assert record["site_state"] == "WI"
    row = upload_record_to_csv_row(record)
    assert row["Site State"] == "WI"
    assert row["Carrier Leasing Source"] == "JF_PermitScraping_jul26"
    assert row["OwnerId"] == "0056O00000EpUOgQAN"

