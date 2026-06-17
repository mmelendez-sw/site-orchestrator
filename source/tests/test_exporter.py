"""Tests for source exporter."""

from source.exporter import export_assets_csv
from source.record import SourceRecord


def test_export_assets_csv_writes_classifier_format(tmp_path):
    records = [
        SourceRecord(
            site_id="wi_001",
            address="100 E PLEASANT ST",
            city="Milwaukee",
            state="WI",
            label="WI",
        )
    ]
    path = export_assets_csv(records, tmp_path / "WI_assets.csv", label="WI")
    text = path.read_text(encoding="utf-8")
    assert "wi_001" in text
    assert "100 E PLEASANT ST" in text
    assert "Milwaukee" in text
    assert "input_confidence" in text
