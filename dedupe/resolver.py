"""Spatial + fuzzy deduplication against Salesforce site records."""

from __future__ import annotations

import math
import os
from typing import Any

from rapidfuzz import fuzz
from simple_salesforce import Salesforce

from dedupe.constants import (
    DEFAULT_RADIUS_METERS,
    DUPLICATE_THRESHOLD,
    REVIEW_THRESHOLD,
    SF_ADDRESS_FIELD,
)
from dedupe.context import build_dataset_context
from dedupe.soql import build_dedupe_query


class SiteResolver:
    """Resolve incoming records against existing Salesforce sites."""

    def __init__(self) -> None:
        username = os.environ["SF_USERNAME"]
        password = os.environ["SF_PASSWORD"]
        security_token = os.environ["SF_SECURITY_TOKEN"]
        domain = os.environ.get("SF_DOMAIN", "login")
        self.sf = Salesforce(
            username=username,
            password=password,
            security_token=security_token,
            domain=domain,
        )
        self._candidate_cache: list[dict[str, Any]] | None = None
        self._dataset_context: dict[str, Any] | None = None

    @staticmethod
    def build_bounding_box(
        lat: float, lng: float, meters: float = DEFAULT_RADIUS_METERS
    ) -> dict[str, float]:
        """Compute a ±meters lat/lng bounding box around a point."""
        delta_lat = meters / 111_320
        delta_lng = meters / (111_320 * math.cos(math.radians(lat)))
        return {
            "min_lat": lat - delta_lat,
            "max_lat": lat + delta_lat,
            "min_lng": lng - delta_lng,
            "max_lng": lng + delta_lng,
        }

    def prefetch(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Load Salesforce candidates for the full dataset zip codes + expanded bbox."""
        self._dataset_context = build_dataset_context(records)
        zip_codes = self._dataset_context["zip_codes"]
        bbox = self._dataset_context["bbox"]
        self._candidate_cache = self.query_salesforce(zip_codes=zip_codes, bbox=bbox)
        return self._candidate_cache

    def query_salesforce(
        self,
        *,
        zip_codes: list[str] | None = None,
        bbox: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Return Salesforce site records for zip codes and/or a bounding box."""
        if zip_codes or bbox:
            soql = build_dedupe_query(zip_codes or [], bbox)
        else:
            raise ValueError("query_salesforce requires zip codes and/or a bounding box")

        result = self.sf.query(soql)
        return list(result.get("records") or [])

    @staticmethod
    def fuzzy_match(
        incoming_address: str, sf_records: list[dict[str, Any]]
    ) -> tuple[int, dict[str, Any] | None]:
        """Score incoming address against candidates; return best score and record."""
        best_score = 0
        best_record: dict[str, Any] | None = None
        for record in sf_records:
            candidate = record.get(SF_ADDRESS_FIELD) or record.get("Name") or ""
            score = fuzz.token_sort_ratio(incoming_address, candidate)
            if score > best_score:
                best_score = score
                best_record = record
        return best_score, best_record

    def resolve(
        self,
        record: dict[str, Any],
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run spatial + fuzzy dedupe and return status, score, and match."""
        lat = record["lat"]
        lng = record["lng"]
        address = record["address"]

        pool = candidates if candidates is not None else self._candidate_cache
        if pool is None:
            bbox = self.build_bounding_box(lat, lng)
            pool = self.query_salesforce(bbox=bbox)

        score, matched = self.fuzzy_match(address, pool)

        if score >= DUPLICATE_THRESHOLD:
            status = "duplicate"
        elif score >= REVIEW_THRESHOLD:
            status = "review"
        else:
            status = "net_new"

        return {
            "status": status,
            "score": score,
            "matched_record": matched,
            "candidate_count": len(pool),
            "dataset_context": self._dataset_context,
        }
