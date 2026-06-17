"""Placeholder tests for ingest normalization paths."""

from ingest.normalizer import normalize
from ingest.scraper import IngestRecord


def test_normalize_geocodes_address_only():
    # TODO: mock geocode() and assert canonical lat/lng/address
    record = IngestRecord(address="1600 Pennsylvania Ave NW, Washington, DC")
    assert record.address is not None


def test_normalize_reverse_geocodes_coords_only():
    # TODO: mock reverse_geocode() and assert formatted address
    record = IngestRecord(lat=38.8977, lng=-77.0365)
    assert record.lat is not None


def test_normalize_validates_address_and_coords():
    # TODO: mock geocode() and assert ValueError when distance exceeds threshold
    record = IngestRecord(
        address="1 Main St, Springfield, IL",
        lat=40.7128,
        lng=-74.0060,
    )
    assert record.address is not None
