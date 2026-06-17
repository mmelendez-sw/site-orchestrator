"""Milwaukee permit source adapter."""

from __future__ import annotations

from typing import Any

from source.adapters.milwaukee.constants import SOURCE_NAME, SOURCE_URL
from source.adapters.milwaukee.enrichment import dedupe_by_address, enrich_permits
from source.adapters.milwaukee.scraper import fetch_telecom_permits
from source.base import BaseSourceAdapter
from source.exporter import source_records_from_dataframe
from source.record import SourceRecord
from source.scope import SourceScope


class MilwaukeeSource(BaseSourceAdapter):
    """Pull telecom permits from Milwaukee open data and enrich with MPROP."""

    name = SOURCE_NAME
    description = "Milwaukee building permits filtered for telecom + MPROP enrichment"

    def supports_scope(self, scope: SourceScope | None) -> bool:
        if scope is None:
            return True
        if scope.country and scope.country.upper() not in {"US", "USA", "UNITED STATES"}:
            return False
        if scope.state and scope.state.upper() not in {"WI", "WISCONSIN"}:
            return False
        if scope.city and scope.city.lower() not in {"milwaukee"}:
            return False
        if scope.county and "milwaukee" not in scope.county.lower():
            return False
        return True

    def fetch(
        self,
        *,
        scope: SourceScope | None = None,
        enrich: bool = True,
        dedupe_addresses: bool = True,
        **_: Any,
    ) -> list[SourceRecord]:
        if scope and not self.supports_scope(scope):
            raise ValueError(
                f"Milwaukee source does not support scope: {scope.to_metadata()}"
            )

        df = fetch_telecom_permits()
        if enrich:
            df = enrich_permits(df)
        if dedupe_addresses:
            df = dedupe_by_address(df)

        effective_scope = scope or SourceScope(
            country="US",
            state="WI",
            county="Milwaukee County",
            city="Milwaukee",
        )
        records = source_records_from_dataframe(
            df,
            source_name=self.name,
            label=effective_scope.label,
            id_prefix=effective_scope.id_prefix,
            source_url=SOURCE_URL,
            zip_col="GEO_ZIP_CODE" if "GEO_ZIP_CODE" in df.columns else None,
        )
        for record in records:
            record.country = effective_scope.country
            record.state = effective_scope.state
            record.county = effective_scope.county
            record.city = effective_scope.city
            record.permit_metadata.update(effective_scope.to_metadata())
        if effective_scope.zip_codes:
            records = [
                record for record in records
                if effective_scope.applies_to_zip(record.zip_code)
            ]
        return records


def fetch(**kwargs: Any) -> list[SourceRecord]:
    """Convenience wrapper for the Milwaukee source adapter."""
    return MilwaukeeSource().fetch(**kwargs)
