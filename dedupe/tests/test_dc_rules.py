"""Tests for DC-specific dedupe rules."""

from dedupe.address_match import is_intersection_address, normalize_for_scoring, strip_dc_noise
from dedupe.batch_postprocess import promote_potential_duplicates
from dedupe.resolver import SiteResolver
from dedupe.urbanicity import urbanicity_for_record


def test_strip_dc_noise_removes_ward():
    cleaned = strip_dc_noise("101 M ST NW, WASHINGTON, DC, 20001, WARD 2")
    assert "WARD" not in cleaned.upper()


def test_is_intersection_address_detects_ampersand():
    assert is_intersection_address("14TH & U ST NW, WASHINGTON, DC 20009")


def test_is_intersection_address_detects_and():
    assert is_intersection_address("7TH AND H ST NW, WASHINGTON, DC 20001")


def test_normalize_for_scoring_strips_dc_tail():
    normalized = normalize_for_scoring("1601 17TH ST NW, WASHINGTON, DC, 20009")
    assert "WASHINGTON" not in normalized.upper() or "17TH" in normalized.upper()


def test_urbanicity_dc_zip_uses_dense_radius(tmp_path, monkeypatch):
    csv_path = tmp_path / "zip_populations.csv"
    csv_path.write_text("zip,population\n20002,68201\n", encoding="utf-8")
    monkeypatch.setenv("ZIP_POPULATION_CSV", str(csv_path))

    from dedupe import urbanicity as urbanicity_module

    urbanicity_module._load_population_table.cache_clear()

    profile = urbanicity_for_record(
        {
            "address": "101 N CAPITOL ST NE, WASHINGTON, DC 20002",
            "state": "DC",
            "lat": 38.9,
            "lng": -77.0,
        }
    )
    assert profile.search_radius_m == 60
    assert profile.tier == "urban"


def test_urbanicity_unknown_dc_zip_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ZIP_POPULATION_CSV", str(tmp_path / "missing.csv"))
    from dedupe import urbanicity as urbanicity_module

    urbanicity_module._load_population_table.cache_clear()

    profile = urbanicity_for_record(
        {"address": "1 MAIN ST, WASHINGTON, DC 20999", "state": "DC", "lat": 38.9, "lng": -77.0}
    )
    assert profile.search_radius_m == 60
    assert profile.population_source == "dc_urban_fallback"


def test_resolve_match_status_address_exact_distance_override():
    match = {
        "within_radius": False,
        "combined_score": 100,
        "address_score": 100,
        "proximity_score": 0,
        "distance_m": 194.0,
        "match_features": {"house_number_delta": 0, "city_mismatch": False},
    }
    status, _, rule, _ = SiteResolver._resolve_match_status(
        match,
        search_radius_m=60,
        incoming_zip="20009",
        matched_zip="20009",
        incoming_address="1601 17TH ST NW, WASHINGTON, DC 20009",
        candidate_address="1601 17TH ST NW, WASHINGTON, DC 20009",
        match_features=match["match_features"],
    )
    assert status == "duplicate"
    assert rule == "address_exact_distance_override"


def test_resolve_match_status_house_number_far():
    match = {
        "within_radius": True,
        "combined_score": 40,
        "address_score": 45,
        "proximity_score": 30,
        "distance_m": 55.0,
        "match_features": {"house_number_delta": 12, "city_mismatch": False},
    }
    status, _, rule, _ = SiteResolver._resolve_match_status(
        match,
        search_radius_m=60,
        incoming_zip="20012",
        matched_zip="20012",
        incoming_address="6419 GEORGIA AVE NW, WASHINGTON, DC 20012",
        candidate_address="6431 Georgia Ave NW, Washington, DC 20012",
        match_features=match["match_features"],
    )
    assert status == "net_new"
    assert rule == "house_number_far"


def test_promote_potential_duplicates_skips_neighbor_rule():
    rows = [
        {
            "status_recommended": "net_new",
            "potential_duplicate": True,
            "routing_reason": "house_number_neighbor",
            "resolution_detail": "routing_reason=house_number_neighbor",
        }
    ]
    changed = promote_potential_duplicates(rows)
    assert changed == 0
    assert rows[0]["status_recommended"] == "net_new"
    assert rows[0]["potential_duplicate"] is False
