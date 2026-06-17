"""Raw permit record from a scraper or manual ingest."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestRecord:
    address: str | None = None
    lat: float | None = None
    lng: float | None = None
    permit_metadata: dict[str, Any] = field(default_factory=dict)
    source_url: str | None = None
