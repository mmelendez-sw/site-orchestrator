"""Tests for Milwaukee source adapter."""

import pandas as pd

from source.adapters.milwaukee.enrichment import normalize_permit_address
from source.adapters.milwaukee.scraper import _find_keywords


def test_normalize_permit_address_strips_city():
    assert normalize_permit_address("2059 S 33RD ST, MILWAUKEE, WI 53215") == "2059 S 33RD ST"


def test_find_keywords_matches_telecom_terms():
    row = pd.Series({"Permit Type": "Antenna", "Use of Building": "Wireless"})
    matched = _find_keywords(row)
    assert "antenna" in matched.lower()


def test_milwaukee_source_registered():
    from source.adapters import ADAPTERS
    assert "milwaukee_permits" in ADAPTERS
