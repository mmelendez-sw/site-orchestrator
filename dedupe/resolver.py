"""Spatial + fuzzy deduplication against Salesforce site records."""

from __future__ import annotations

import math
import os
from typing import Any

from rapidfuzz import fuzz
from simple_salesforce import Salesforce

from dedupe.constants import (
    ADDRESS_SCORE_WEIGHT,
    DEFAULT_RADIUS_METERS,
    DUPLICATE_THRESHOLD,
    PROXIMITY_SCORE_WEIGHT,
    REVIEW_THRESHOLD,
    SF_ADDRESS_FIELD,
    SF_LAT_FIELD,
    SF_LNG_FIELD,
)
from dedupe.context import build_dataset_context
from dedupe.soql import build_dedupe_query
from dedupe.spatial import (
    combined_score,
    haversine_meters,
    proximity_score,
    sf_coordinates,
)
from dedupe.urbanicity import UrbanicityProfile, urbanicity_for_record


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

    @staticmethod
    def _score_candidate(
        incoming_address: str,
        incoming_lat: float,
        incoming_lng: float,
        sf_record: dict[str, Any],
        *,
        search_radius_m: float,
    ) -> dict[str, Any]:
        candidate_address = sf_record.get(SF_ADDRESS_FIELD) or sf_record.get("Name") or ""
        address_score = fuzz.token_sort_ratio(incoming_address, candidate_address)
        coords = sf_coordinates(sf_record, lat_field=SF_LAT_FIELD, lng_field=SF_LNG_FIELD)

        if coords is None:
            return {
                "record": sf_record,
                "address_score": address_score,
                "distance_m": None,
                "within_radius": False,
                "proximity_score": 0,
                "combined_score": address_score,
            }

        distance_m = haversine_meters(incoming_lat, incoming_lng, coords[0], coords[1])
        within_radius = distance_m <= search_radius_m
        prox = proximity_score(distance_m, search_radius_m) if within_radius else 0
        combined = (
            combined_score(
                address_score,
                prox,
                address_weight=ADDRESS_SCORE_WEIGHT,
                proximity_weight=PROXIMITY_SCORE_WEIGHT,
            )
            if within_radius
            else address_score
        )
        return {
            "record": sf_record,
            "address_score": address_score,
            "distance_m": distance_m,
            "within_radius": within_radius,
            "proximity_score": prox,
            "combined_score": combined,
        }

    @staticmethod
    def _pick_best(candidates: list[dict[str, Any]], *, key: str) -> dict[str, Any] | None:
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[key])

    @staticmethod
    def _status_from_score(score: int) -> str:
        if score >= DUPLICATE_THRESHOLD:
            return "duplicate"
        if score >= REVIEW_THRESHOLD:
            return "review"
        return "net_new"

    @staticmethod
    def _build_resolution_detail(
        *,
        urbanicity: UrbanicityProfile,
        spatial_candidate_count: int,
        match: dict[str, Any] | None,
        status: str,
        used_outside_radius_match: bool,
    ) -> str:
        radius = int(urbanicity.search_radius_m)
        pop = urbanicity.population
        pop_text = f"{pop:,}" if pop is not None else "unknown"
        if match is None:
            return (
                f"{urbanicity.tier} zip population={pop_text} radius={radius}m "
                f"spatial_candidates=0/{spatial_candidate_count}; no Salesforce match"
            )

        distance_text = (
            f"{match['distance_m']:.0f}m"
            if match.get("distance_m") is not None
            else "no_sf_coordinates"
        )
        detail = (
            f"{urbanicity.tier} zip population={pop_text} radius={radius}m "
            f"spatial_candidates={spatial_candidate_count}; "
            f"address_score={match['address_score']} proximity_score={match['proximity_score']} "
            f"combined_score={match['combined_score']} distance={distance_text}"
        )
        if used_outside_radius_match:
            detail += "; address_match_outside_radius"
        detail += f"; status={status}"
        return detail

    def resolve(
        self,
        record: dict[str, Any],
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run urbanicity-radius spatial gate + fuzzy dedupe and return status."""
        address = record["address"]
        incoming_lat = float(record["lat"])
        incoming_lng = float(record["lng"])
        urbanicity = urbanicity_for_record(record)

        pool = candidates if candidates is not None else self._candidate_cache
        if pool is None:
            raise RuntimeError(
                "Call prefetch() with the full dataset before resolve(). "
                "Dedupe uses one expanded bounding box from the dataset min/max "
                "lat/lng, not a per-site radius."
            )

        scored = [
            self._score_candidate(
                address,
                incoming_lat,
                incoming_lng,
                sf_record,
                search_radius_m=urbanicity.search_radius_m,
            )
            for sf_record in pool
        ]
        in_radius = [item for item in scored if item["within_radius"]]
        spatial_candidate_count = len(in_radius)
        best_in_radius = self._pick_best(in_radius, key="combined_score")
        best_outside = self._pick_best(
            [item for item in scored if not item["within_radius"]],
            key="address_score",
        )

        used_outside_radius_match = False
        if best_in_radius is not None:
            match = best_in_radius
            score = match["combined_score"]
            status = self._status_from_score(score)
        elif (
            best_outside is not None
            and best_outside["address_score"] >= REVIEW_THRESHOLD
        ):
            match = best_outside
            score = match["address_score"]
            status = "review"
            used_outside_radius_match = True
        else:
            match = best_outside
            score = match["address_score"] if match else 0
            status = "net_new"

        matched_record = match["record"] if match else None
        resolution_detail = self._build_resolution_detail(
            urbanicity=urbanicity,
            spatial_candidate_count=spatial_candidate_count,
            match=match,
            status=status,
            used_outside_radius_match=used_outside_radius_match,
        )

        return {
            "status": status,
            "score": score,
            "address_score": match["address_score"] if match else 0,
            "combined_score": match["combined_score"] if match else 0,
            "proximity_score": match["proximity_score"] if match else 0,
            "matched_distance_m": match["distance_m"] if match else None,
            "matched_record": matched_record,
            "candidate_count": len(pool),
            "spatial_candidate_count": spatial_candidate_count,
            "urbanicity": urbanicity.as_dict(),
            "resolution_detail": resolution_detail,
            "used_outside_radius_match": used_outside_radius_match,
            "dataset_context": self._dataset_context,
        }
