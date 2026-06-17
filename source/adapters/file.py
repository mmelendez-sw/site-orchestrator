"""Load candidate sites from CSV or JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from source.base import BaseSourceAdapter
from source.exporter import source_records_from_dataframe
from source.record import SourceRecord
from source.scope import SourceScope

SOURCE_NAME = "file"


class FileSource(BaseSourceAdapter):
    """Load hand-built, Claude-generated, or script-generated candidate lists."""

    name = SOURCE_NAME
    description = "CSV/JSON file input with optional geographic scope filters"

    def supports_scope(self, scope: SourceScope | None) -> bool:
        return True

    def fetch(
        self,
        *,
        scope: SourceScope | None = None,
        input_path: str | Path,
        **_: Any,
    ) -> list[SourceRecord]:
        path = Path(input_path)
        if not path.exists():
            raise FileNotFoundError(f"Input file not found: {path}")

        if path.suffix.lower() == ".json":
            records = self._load_json(path, scope)
        else:
            records = self._load_csv(path, scope)
        return self._apply_scope(records, scope)

    def _load_csv(self, path: Path, scope: SourceScope | None) -> list[SourceRecord]:
        df = pd.read_csv(path)
        label = scope.label if scope else None
        id_prefix = scope.id_prefix if scope else path.stem.lower()
        zip_col = _find_column(df, ("zip_code", "zip", "postal_code", "GEO_ZIP_CODE"))
        county_col = _find_column(df, ("county", "County"))
        country_col = _find_column(df, ("country", "Country"))

        records = source_records_from_dataframe(
            df,
            source_name=self.name,
            label=label,
            id_prefix=id_prefix,
            source_url=str(path),
            zip_col=zip_col,
            county_col=county_col,
            country_col=country_col,
        )
        if scope:
            for record in records:
                record.country = record.country or scope.country
                record.state = record.state or scope.state
                record.county = record.county or scope.county
                record.city = record.city or scope.city
                record.permit_metadata.update(scope.to_metadata())
        return records

    def _load_json(self, path: Path, scope: SourceScope | None) -> list[SourceRecord]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("records", [])
        records: list[SourceRecord] = []
        for idx, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            address = str(row.get("address") or row.get("Address") or "").strip()
            if not address:
                continue
            prefix = scope.id_prefix if scope else "site"
            records.append(SourceRecord(
                site_id=row.get("site_id") or row.get("id") or f"{prefix}_{idx:03d}",
                address=address,
                city=row.get("city") or (scope.city if scope else None),
                state=row.get("state") or (scope.state if scope else None),
                county=row.get("county") or (scope.county if scope else None),
                country=row.get("country") or (scope.country if scope else None),
                zip_code=row.get("zip_code") or row.get("zip"),
                lat=row.get("lat"),
                lng=row.get("lng"),
                label=row.get("label") or (scope.label if scope else None),
                input_confidence=row.get("input_confidence", "high"),
                permit_metadata={
                    **(scope.to_metadata() if scope else {}),
                    **dict(row.get("permit_metadata") or {}),
                },
                source_name=self.name,
                source_url=str(path),
            ))
        return records

    @staticmethod
    def _apply_scope(
        records: list[SourceRecord],
        scope: SourceScope | None,
    ) -> list[SourceRecord]:
        if scope is None:
            return records
        filtered = [
            record for record in records
            if scope.applies_to_zip(record.zip_code)
            and scope.matches(
                country=record.country,
                state=record.state,
                county=record.county,
                city=record.city,
            )
        ]
        return filtered


def _find_column(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    lower_map = {col.lower(): col for col in df.columns}
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return None
