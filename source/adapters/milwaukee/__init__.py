"""Milwaukee permit source adapter."""

from __future__ import annotations

from typing import Any

from source.adapters.milwaukee.constants import SOURCE_NAME, SOURCE_URL
from source.adapters.milwaukee.enrichment import dedupe_by_address, enrich_permits
from source.adapters.milwaukee.scraper import fetch_telecom_permits
from source.base import BaseSourceAdapter
from source.exporter import source_records_from_dataframe
from source.record import SourceRecord


class MilwaukeeSource(BaseSourceAdapter):
    """Pull telecom permits from Milwaukee open data and enrich with MPROP."""

    name = SOURCE_NAME
    description = "Milwaukee building permits filtered for telecom + MPROP enrichment"

    def fetch(
        self,
        *,
        enrich: bool = True,
        dedupe_addresses: bool = True,
        **_: Any,
    ) -> list[SourceRecord]:
        df = fetch_telecom_permits()
        if enrich:
            df = enrich_permits(df)
        if dedupe_addresses:
            df = dedupe_by_address(df)
        return source_records_from_dataframe(
            df,
            source_name=self.name,
            label="WI",
            id_prefix="wi",
            source_url=SOURCE_URL,
        )


def fetch(**kwargs: Any) -> list[SourceRecord]:
    """Convenience wrapper for the Milwaukee source adapter."""
    return MilwaukeeSource().fetch(**kwargs)
