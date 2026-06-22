"""Tests for Site_Type__c mapping from classifier and permit text."""

from salesforce.site_type_mapping import (
    map_site_type_for_upload,
    normalize_tower_subtype,
    site_type_from_permit_text,
)


def test_normalize_tower_subtype_aliases():
    assert normalize_tower_subtype("self-support") == "self_support"
    assert normalize_tower_subtype("lattice") == "self_support"


def test_map_tower_subtype_to_salesforce():
    assert map_site_type_for_upload(
        {"site_type": "tower", "tower_subtype": "monopole"}
    ) == "Monopole"
    assert map_site_type_for_upload(
        {"site_type": "tower", "tower_subtype": "guyed"}
    ) == "Guyed Tower"
    assert map_site_type_for_upload(
        {"site_type": "tower", "tower_subtype": "stealth"}
    ) == "Stealth"


def test_map_rooftop():
    assert map_site_type_for_upload({"site_type": "rooftop"}) == "Rooftop"


def test_permit_text_fallback_for_tower_without_subtype():
    assert map_site_type_for_upload(
        {"site_type": "tower", "tower_subtype": "unclear"},
        permit_metadata={"description": "Install new monopole wireless facility"},
    ) == "Monopole"


def test_site_type_from_permit_text_small_cell():
    assert site_type_from_permit_text("5G small cell on utility pole") == "Small Cell"
