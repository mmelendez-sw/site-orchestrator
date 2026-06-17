"""Spatial + fuzzy deduplication against Salesforce site records."""

from __future__ import annotations

import math
import os
from typing import Any

from rapidfuzz import fuzz
from simple_salesforce import Salesforce

from dedup.constants import (
    DEFAULT_RADIUS_METERS,
    DUPLICATE_THRESHOLD,
    REVIEW_THRESHOLD,
)
from dedup.soql import build_bbox_query


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

    def query_salesforce(self, bbox: dict[str, float]) -> list[dict[str, Any]]:
        """Return Salesforce site records within the bounding box."""
        soql = build_bbox_query(
            bbox["min_lat"],
            bbox["max_lat"],
            bbox["min_lng"],
            bbox["max_lng"],
        )
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
            candidate = record.get("Address__c") or record.get("Name") or ""
            score = fuzz.token_sort_ratio(incoming_address, candidate)
            if score > best_score:
                best_score = score
                best_record = record
        return best_score, best_record

    def resolve(self, record: dict[str, Any]) -> dict[str, Any]:
        """Run spatial + fuzzy dedup and return status, score, and match."""
        lat = record["lat"]
        lng = record["lng"]
        address = record["address"]

        bbox = self.build_bounding_box(lat, lng)
        candidates = self.query_salesforce(bbox)
        score, matched = self.fuzzy_match(address, candidates)

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
        }
