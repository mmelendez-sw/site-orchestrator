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
    OUTSIDE_RADIUS_REVIEW_MAX_M,
    PROXIMITY_SCORE_WEIGHT,
    REVIEW_THRESHOLD,
    SF_ADDRESS_FIELD,
    SF_LAT_FIELD,
    SF_LNG_FIELD,
    SF_ZIP_FIELD,
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

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        username = os.environ["SF_USERNAME"]
        password = os.environ["SF_PASSWORD"]
        security_token = os.environ["SF_SECURITY_TOKEN"]
        domain = os.environ.get("SF_DOMAIN", "login")
        login_host = "test.salesforce.com" if domain == "test" else (
            "login.salesforce.com" if domain == "login" else f"{domain}.salesforce.com"
        )

        import logging
        logger = logging.getLogger(__name__)
        if verbose:
            logger.info("=" * 72)
            logger.info("SALESFORCE CONNECT")
            logger.info("  login host : https://%s", login_host)
            logger.info("  username   : %s", username)
            logger.info("  domain env : %s", domain)
            logger.info("  (.uat username → Salesforce routes to your UAT sandbox org)")
            logger.info("=" * 72)

        self.sf = Salesforce(
            username=username,
            password=password,
            security_token=security_token,
            domain=domain,
        )
        self._candidate_cache: list[dict[str, Any]] | None = None
        self._dataset_context: dict[str, Any] | None = None

        if verbose:
            logger.info(
                "Salesforce authenticated — API instance: https://%s",
                self.sf.sf_instance,
            )
            logger.info("All Site__c queries run against this org instance.")

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
        import logging
        logger = logging.getLogger(__name__)

        self._dataset_context = build_dataset_context(records)
        zip_codes = self._dataset_context["zip_codes"]
        bbox = self._dataset_context["bbox"]

        if self.verbose:
            logger.info("=" * 72)
            logger.info("SALESFORCE PREFETCH (bulk candidate query)")
            logger.info("  normalized records : %d", len(records))
            logger.info("  unique zip codes   : %d", len(zip_codes))
            if zip_codes:
                preview = ", ".join(zip_codes[:15])
                if len(zip_codes) > 15:
                    preview += f", ... (+{len(zip_codes) - 15} more)"
                logger.info("  zips               : %s", preview)
            if bbox:
                logger.info(
                    "  dataset bbox (+250m buffer): lat [%.5f, %.5f] lng [%.5f, %.5f]",
                    bbox["min_lat"],
                    bbox["max_lat"],
                    bbox["min_lng"],
                    bbox["max_lng"],
                )

        soql = build_dedupe_query(zip_codes or [], bbox)
        if self.verbose:
            logger.info("  SOQL: %s", soql)
            logger.info("  executing query...")

        self._candidate_cache = self.query_salesforce(zip_codes=zip_codes, bbox=bbox)

        if self.verbose:
            with_coords = sum(
                1
                for row in self._candidate_cache
                if row.get(SF_LAT_FIELD) is not None and row.get(SF_LNG_FIELD) is not None
            )
            logger.info(
                "  returned %d Site__c rows (%d with lat/lng for spatial matching)",
                len(self._candidate_cache),
                with_coords,
            )
            logger.info("=" * 72)

        return self._candidate_cache

    def query_salesforce(
        self,
        *,
        zip_codes: list[str] | None = None,
        bbox: dict[str, float] | None = None,
        soql: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return Salesforce site records for zip codes and/or a bounding box."""
        if soql is None:
            if zip_codes or bbox:
                soql = build_dedupe_query(zip_codes or [], bbox)
            else:
                raise ValueError("query_salesforce requires zip codes and/or a bounding box")

        try:
            result = self.sf.query(soql)
            return list(result.get("records") or [])
        except Exception as exc:
            import logging
            logger = logging.getLogger(__name__)
            logger.error("Salesforce SOQL query failed: %s", exc)
            logger.error("SOQL was: %s", soql)
            raise

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
    def _normalize_sf_address(value: Any) -> str:
        text = str(value or "")
        return text.replace("<br>", " ").replace("<BR>", " ").strip()

    @staticmethod
    def _normalize_zip(value: Any) -> str | None:
        if value is None:
            return None
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if len(digits) >= 5:
            return digits[:5]
        return None

    @staticmethod
    def _outside_radius_review_cap(search_radius_m: float) -> float:
        return max(search_radius_m * 3, float(OUTSIDE_RADIUS_REVIEW_MAX_M))

    @staticmethod
    def _resolve_outside_radius_match(
        best_outside: dict[str, Any],
        *,
        incoming_zip: str | None,
        search_radius_m: float,
    ) -> tuple[str, int, bool]:
        """Decide status for the best address match outside the urbanicity circle."""
        score = best_outside["address_score"]
        matched_zip = SiteResolver._normalize_zip(best_outside["record"].get(SF_ZIP_FIELD))
        distance_m = best_outside.get("distance_m")
        max_review_m = SiteResolver._outside_radius_review_cap(search_radius_m)

        if distance_m is not None and distance_m > max_review_m:
            return "net_new", score, False

        if distance_m is None:
            if not (incoming_zip and matched_zip and incoming_zip == matched_zip):
                return "net_new", score, False
            if score >= DUPLICATE_THRESHOLD:
                return "duplicate", score, True
            if score >= REVIEW_THRESHOLD:
                return "review", score, True
            return "net_new", score, False

        if score >= DUPLICATE_THRESHOLD:
            return "duplicate", score, True
        if score >= REVIEW_THRESHOLD:
            return "review", score, True
        return "net_new", score, False

    @staticmethod
    def _score_candidate(
        incoming_address: str,
        incoming_lat: float,
        incoming_lng: float,
        sf_record: dict[str, Any],
        *,
        search_radius_m: float,
    ) -> dict[str, Any]:
        candidate_address = SiteResolver._normalize_sf_address(
            sf_record.get(SF_ADDRESS_FIELD) or sf_record.get("Name") or ""
        )
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
        elif (
            match is not None
            and match.get("distance_m") is not None
            and match["distance_m"] > urbanicity.search_radius_m
        ):
            detail += "; address_match_too_far_for_review"
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
            status, score, used_outside_radius_match = SiteResolver._resolve_outside_radius_match(
                best_outside,
                incoming_zip=self._normalize_zip(record.get("zip_code")),
                search_radius_m=urbanicity.search_radius_m,
            )
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

        if self.verbose:
            import logging
            logger = logging.getLogger(__name__)
            matched = matched_record or {}
            matched_addr = matched.get(SF_ADDRESS_FIELD) or matched.get("Name") or "—"
            logger.info(
                "    urbanicity : %s (zip=%s pop=%s radius=%sm)",
                urbanicity.tier,
                urbanicity.zip_code or "—",
                f"{urbanicity.population:,}" if urbanicity.population else "unknown",
                int(urbanicity.search_radius_m),
            )
            logger.info(
                "    candidates : %d total in prefetch pool, %d within radius",
                len(pool),
                spatial_candidate_count,
            )
            if match:
                dist = (
                    f"{match['distance_m']:.0f}m"
                    if match.get("distance_m") is not None
                    else "no_sf_coordinates"
                )
                logger.info(
                    "    best match : %s | %s",
                    matched.get("Id", "—"),
                    matched_addr[:80],
                )
                logger.info(
                    "    scores     : address=%s proximity=%s combined=%s distance=%s",
                    match["address_score"],
                    match["proximity_score"],
                    match["combined_score"],
                    dist,
                )
            logger.info("    result     : %s — %s", status.upper(), resolution_detail)

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
