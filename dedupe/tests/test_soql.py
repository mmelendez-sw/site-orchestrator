"""Tests for dedupe SOQL builders."""

from dedupe.soql import build_dedupe_query


def test_build_dedupe_query_uses_site_lat_lng_fields():
    query = build_dedupe_query(
        ["53202", "53203"],
        {"min_lat": 43.0, "max_lat": 43.1, "min_lng": -88.0, "max_lng": -87.9},
    )
    assert "Site_Latitude__c" in query
    assert "Site_Longitude__c" in query
    assert "Zip_Code__c IN ('53202', '53203')" in query
    assert "Site_Latitude__c >= 43.0" in query
    assert " OR " in query


def test_build_dedupe_query_zip_only():
    query = build_dedupe_query(["20001"], None)
    assert "WHERE Zip_Code__c IN ('20001')" in query
    assert "Site_Latitude__c >=" not in query
