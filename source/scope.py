"""Geographic scope for permit source discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceScope:
    """Geographic filters: country, state, county, city, and/or zip codes."""

    country: str | None = None
    state: str | None = None
    county: str | None = None
    city: str | None = None
    zip_codes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.country = _clean(self.country)
        self.state = _clean(self.state)
        self.county = _clean(self.county)
        self.city = _clean(self.city)
        self.zip_codes = [_normalize_zip(z) for z in self.zip_codes if _normalize_zip(z)]

    @property
    def label(self) -> str:
        """Default classifier label derived from scope."""
        if self.state:
            return self.state.upper()
        if self.country:
            return self.country.upper()
        return "site"

    @property
    def id_prefix(self) -> str:
        if self.city:
            return self.city.lower().replace(" ", "_")
        if self.state:
            return self.state.lower()
        if self.country:
            return self.country.lower()
        return "site"

    def matches(
        self,
        *,
        country: str | None = None,
        state: str | None = None,
        county: str | None = None,
        city: str | None = None,
    ) -> bool:
        """Return True when this scope is compatible with a jurisdiction."""
        checks = (
            (self.country, country),
            (self.state, state),
            (self.county, county),
            (self.city, city),
        )
        for scope_val, target_val in checks:
            if scope_val and target_val and scope_val.lower() != target_val.lower():
                return False
        return True

    def applies_to_zip(self, zip_code: str | None) -> bool:
        """Return True when no zip filter is set or the zip is included."""
        if not self.zip_codes:
            return True
        normalized = _normalize_zip(zip_code)
        return normalized in self.zip_codes if normalized else False

    def to_metadata(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "scope_country": self.country,
                "scope_state": self.state,
                "scope_county": self.county,
                "scope_city": self.city,
                "scope_zip_codes": self.zip_codes or None,
            }.items()
            if value
        }


def parse_scope(
    *,
    country: str | None = None,
    state: str | None = None,
    county: str | None = None,
    city: str | None = None,
    zip_codes: str | list[str] | None = None,
) -> SourceScope:
    """Build a SourceScope from CLI or programmatic inputs."""
    zips: list[str] = []
    if isinstance(zip_codes, str):
        zips = [part.strip() for part in zip_codes.split(",") if part.strip()]
    elif zip_codes:
        zips = list(zip_codes)
    return SourceScope(
        country=country,
        state=state,
        county=county,
        city=city,
        zip_codes=zips,
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_zip(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[:5] if len(digits) >= 5 else digits
