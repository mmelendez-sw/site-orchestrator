"""Canonical record produced by a permit source adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ingest.scraper import IngestRecord


@dataclass
class SourceRecord:
    """A candidate site from open permit data or AI-generated research."""

    address: str
    city: str | None = None
    state: str | None = None
    county: str | None = None
    country: str | None = None
    zip_code: str | None = None
    lat: float | None = None
    lng: float | None = None
    permit_metadata: dict[str, Any] = field(default_factory=dict)
    source_name: str | None = None
    source_url: str | None = None
    site_id: str | None = None
    label: str | None = None
    input_confidence: str = "high"

    @property
    def full_address(self) -> str:
        """Return a full address string suitable for geocoding."""
        addr = self.address.strip()
        suffix_parts = [part for part in (self.city, self.state, self.zip_code) if part]
        if suffix_parts:
            suffix = ", ".join(suffix_parts)
            if suffix.upper() not in addr.upper():
                return f"{addr}, {suffix}"
        elif self.city and self.state:
            city_state = f"{self.city}, {self.state}"
            if city_state.upper() not in addr.upper():
                return f"{addr}, {city_state}"
        return addr

    def to_ingest_record(self) -> IngestRecord:
        """Hand off to the ingest normalizer."""
        metadata = dict(self.permit_metadata)
        if self.source_name:
            metadata.setdefault("source_name", self.source_name)
        if self.site_id:
            metadata.setdefault("site_id", self.site_id)
        if self.label:
            metadata.setdefault("label", self.label)
        if self.zip_code:
            metadata.setdefault("zip_code", self.zip_code)
        if self.county:
            metadata.setdefault("county", self.county)
        if self.country:
            metadata.setdefault("country", self.country)
        return IngestRecord(
            address=self.full_address,
            lat=self.lat,
            lng=self.lng,
            permit_metadata=metadata,
            source_url=self.source_url,
        )
