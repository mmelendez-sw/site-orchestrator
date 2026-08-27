"""Tests for NAIP box helpers (no network / paid APIs)."""

from classifier.asset_classifier import (
    coerce_asset_box,
    locate_asset_box_latlon,
    normalize_model_result,
)


def test_coerce_asset_box_accepts_tight_antenna_box():
    tight = [480, 470, 530, 520]
    assert coerce_asset_box(tight) == tight
    assert coerce_asset_box([530, 520, 480, 470]) == tight
    assert coerce_asset_box([0, 0, 900, 900]) is None


def test_normalize_model_result_clamps_confidence():
    res = normalize_model_result(
        {"site_type": "tower", "tower_subtype": "monopole", "site_confidence": 92}
    )
    assert res["site_confidence"] == 0.92


def test_locate_asset_box_from_naip_geo():
    geo = {
        "crs": "EPSG:3857",
        "x_min": 0.0,
        "x_max": 250.0,
        "y_min": 0.0,
        "y_max": 250.0,
        "chip_m": 250.0,
    }
    located = locate_asset_box_latlon(
        lat=40.0,
        lon=-74.0,
        box=[200, 700, 300, 800],
        box_view="NAIP top-down",
        naip_geo=geo,
    )
    assert located is not None
    _alat, _alon, offset_m, source = located
    assert source == "naip_asset_box"
    assert offset_m > 0
