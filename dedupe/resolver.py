"""Spatial + fuzzy deduplication against Salesforce site records."""

from __future__ import annotations

import math
import os
from typing import Any

from simple_salesforce import Salesforce

from dedupe.address_match import (
    address_match_score,
    city_mismatch_for_review,
    evaluate_match_features,
    extract_city_from_address,
    house_number_neighbor_auto_net_new,
    is_intersection_address,
    normalize_for_scoring,
    normalize_sf_address,
    street_token_jaccard,
)
from dedupe.constants import (
    ADDRESS_FLOOR_PROXIMITY_MIN,
    ADDRESS_FLOOR_SCORE,
    ADDRESS_SCORE_WEIGHT,
    AUTO_REJECT_ADDRESS_SCORE,
    CITY_MISMATCH_REVIEW_MIN_COMBINED,
    DEFAULT_RADIUS_METERS,
    DUPLICATE_THRESHOLD,
    FUZZY_PREFILTER_MAX_M,
    GEOCODER_COLLISION_JACCARD_MIN,
    GEOCODER_COLLISION_MAX_ADDRESS,
    GEOCODER_COLLISION_MAX_M,
    HIGH_ADDRESS_EXACT_MIN,
    HIGH_ADDRESS_RADIUS_MULTIPLIER,
    HIGH_ADDRESS_STRONG_MIN,
    HOUSE_NUMBER_DELTA_PENALTY_START,
    HOUSE_NUMBER_FAR_DELTA_MIN,
    HOUSE_NUMBER_FAR_DISTANCE_M,
    POTENTIAL_DUPLICATE_BORDERLINE_MAX,
    POTENTIAL_DUPLICATE_BORDERLINE_MIN,
    POTENTIAL_DUPLICATE_MAX_DISTANCE_M,
    POTENTIAL_DUPLICATE_MIN_COMBINED,
    PROX_DUPLICATE_MAX_M,
    PROX_DUPLICATE_MIN_ADDRESS,
    PROX_REVIEW_EXTENDED_MAX_M,
    PROX_REVIEW_EXTENDED_MIN_ADDRESS,
    PROX_REVIEW_MAX_M,
    PROX_REVIEW_MIN_ADDRESS,
    PROXIMITY_DOWNWEIGHT_ADDRESS_MAX,
    PROXIMITY_SCORE_WEIGHT,
    REVIEW_THRESHOLD,
    SCORING_MODE_ADDRESS_EXACT,
    SCORING_MODE_WEIGHTED,
    SF_ADDRESS_FIELD,
    SF_CITY_FIELD,
    SF_LAT_FIELD,
    SF_LNG_FIELD,
    SF_ZIP_FIELD,
    THRESHOLD_VERSION,
    TIE_BREAKER_CLOSE_MAX_DELTA,
    TOP_CANDIDATES_MAX,
    TOP_CANDIDATES_MIN_PREFILTER,
    ZIP_MISMATCH_REVIEW_MAX_M,
)
from dedupe.context import build_dataset_context
from dedupe.match_snapshot import serialize_scored_candidate, top_candidates_json
from dedupe.soql import build_dedupe_query
from dedupe.sf_geocode import enrich_missing_sf_coordinates, resolve_sf_coordinates
from dedupe.spatial import combined_score, haversine_meters, proximity_score
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
        enrich_missing_sf_coordinates(self._candidate_cache, verbose=self.verbose)

        if self.verbose:
            with_coords = sum(
                1 for row in self._candidate_cache if resolve_sf_coordinates(row) is not None
            )
            logger.info(
                "  returned %d Site__c rows (%d with coordinates for spatial matching)",
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
            score = address_match_score(incoming_address, candidate)
            if score > best_score:
                best_score = score
                best_record = record
        return best_score, best_record

    @staticmethod
    def _normalize_zip(value: Any) -> str | None:
        if value is None:
            return None
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if len(digits) >= 5:
            return digits[:5]
        return None

    @staticmethod
    def _prefilter_candidates(
        pool: list[dict[str, Any]],
        *,
        incoming_lat: float,
        incoming_lng: float,
        incoming_zip: str | None,
        max_distance_m: float,
    ) -> list[dict[str, Any]]:
        """Keep only nearby Salesforce rows (or same-zip rows missing coordinates)."""
        filtered: list[dict[str, Any]] = []
        for record in pool:
            resolved = resolve_sf_coordinates(record)
            if resolved is not None:
                lat, lng, _ = resolved
                if haversine_meters(incoming_lat, incoming_lng, lat, lng) <= max_distance_m:
                    filtered.append(record)
                continue

            matched_zip = SiteResolver._normalize_zip(record.get(SF_ZIP_FIELD))
            if incoming_zip and matched_zip and incoming_zip == matched_zip:
                filtered.append(record)

        return filtered

    @staticmethod
    def _apply_house_number_delta_penalty(
        combined: int,
        *,
        house_number_delta: int | None,
    ) -> int:
        if house_number_delta is None:
            return combined
        if house_number_delta <= HOUSE_NUMBER_DELTA_PENALTY_START:
            return combined
        penalty = house_number_delta * 2
        return max(0, combined - penalty)

    @staticmethod
    def _downweight_proximity(address_score: int, proximity: int) -> int:
        if address_score >= PROXIMITY_DOWNWEIGHT_ADDRESS_MAX:
            return proximity
        scale = max(0.0, address_score / PROXIMITY_DOWNWEIGHT_ADDRESS_MAX)
        return int(round(proximity * scale))

    @staticmethod
    def _score_candidate(
        incoming_address: str,
        incoming_lat: float,
        incoming_lng: float,
        sf_record: dict[str, Any],
        *,
        search_radius_m: float,
        incoming_city: str | None = None,
    ) -> dict[str, Any]:
        candidate_address = normalize_sf_address(
            sf_record.get(SF_ADDRESS_FIELD) or sf_record.get("Name") or ""
        )
        address_score = address_match_score(incoming_address, candidate_address)
        resolved = resolve_sf_coordinates(sf_record)
        matched_city = sf_record.get(SF_CITY_FIELD)

        if resolved is None:
            features = evaluate_match_features(
                incoming_address,
                candidate_address,
                incoming_city=incoming_city,
                matched_city=matched_city,
                distance_m=None,
            )
            return {
                "record": sf_record,
                "address_score": address_score,
                "distance_m": None,
                "within_radius": False,
                "proximity_score": 0,
                "combined_score": address_score,
                "coordinate_source": "missing",
                "scoring_mode": SCORING_MODE_WEIGHTED,
                "match_features": features,
            }

        lat, lng, coordinate_source = resolved
        # Geocoded SF fallback shares the incoming pin — proximity is not meaningful.
        if coordinate_source == "geocoded":
            features = evaluate_match_features(
                incoming_address,
                candidate_address,
                incoming_city=incoming_city,
                matched_city=matched_city,
                distance_m=None,
            )
            return {
                "record": sf_record,
                "address_score": address_score,
                "distance_m": None,
                "within_radius": False,
                "proximity_score": None,
                "combined_score": address_score,
                "coordinate_source": coordinate_source,
                "scoring_mode": SCORING_MODE_WEIGHTED,
                "match_features": features,
            }

        distance_m = haversine_meters(incoming_lat, incoming_lng, lat, lng)
        within_radius = distance_m <= search_radius_m
        raw_prox = proximity_score(distance_m, search_radius_m) if within_radius else 0
        prox = SiteResolver._downweight_proximity(address_score, raw_prox)
        # address_exact_override: strong address matches pin combined_score to
        # address_score and ignore proximity (documented in README scoring_mode).
        if address_score >= HIGH_ADDRESS_STRONG_MIN:
            combined = address_score
            scoring_mode = SCORING_MODE_ADDRESS_EXACT
        elif within_radius:
            combined = combined_score(
                address_score,
                prox,
                address_weight=ADDRESS_SCORE_WEIGHT,
                proximity_weight=PROXIMITY_SCORE_WEIGHT,
            )
            scoring_mode = SCORING_MODE_WEIGHTED
        else:
            combined = address_score
            scoring_mode = SCORING_MODE_WEIGHTED

        features = evaluate_match_features(
            incoming_address,
            candidate_address,
            incoming_city=incoming_city,
            matched_city=matched_city,
            distance_m=distance_m,
        )
        combined = SiteResolver._apply_house_number_delta_penalty(
            combined,
            house_number_delta=features["house_number_delta"],
        )

        return {
            "record": sf_record,
            "address_score": address_score,
            "distance_m": distance_m,
            "within_radius": within_radius,
            "proximity_score": prox,
            "combined_score": combined,
            "coordinate_source": coordinate_source,
            "scoring_mode": scoring_mode,
            "match_features": features,
        }

    @staticmethod
    def _eligible_for_resolution(
        scored: list[dict[str, Any]],
        *,
        search_radius_m: float,
    ) -> list[dict[str, Any]]:
        """Candidates that can influence duplicate/review/net-new status."""
        max_review_m = search_radius_m * HIGH_ADDRESS_RADIUS_MULTIPLIER
        eligible: list[dict[str, Any]] = []
        for item in scored:
            distance_m = item.get("distance_m")
            address_score = item["address_score"]
            if distance_m is None:
                continue
            if item["within_radius"]:
                eligible.append(item)
                continue
            if address_score >= HIGH_ADDRESS_STRONG_MIN and distance_m <= max_review_m:
                eligible.append(item)
                continue
            if (
                distance_m < GEOCODER_COLLISION_MAX_M
                and address_score < GEOCODER_COLLISION_MAX_ADDRESS
            ):
                eligible.append(item)
        return eligible

    @staticmethod
    def _gate_passing_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            item
            for item in candidates
            if (item.get("match_features") or {}).get("passed", True)
        ]

    @staticmethod
    def _resolve_match_status(
        match: dict[str, Any],
        *,
        search_radius_m: float,
        incoming_zip: str | None,
        matched_zip: str | None,
        incoming_address: str,
        candidate_address: str,
        match_features: dict[str, Any] | None = None,
    ) -> tuple[str, int, str | None, bool]:
        """Return status, score, routing_reason, and zip_mismatch flag."""
        combined = match["combined_score"]
        address_score = match["address_score"]
        proximity_score_value = match["proximity_score"]
        distance_m = match.get("distance_m")
        max_review_m = search_radius_m * HIGH_ADDRESS_RADIUS_MULTIPLIER
        zip_mismatch = bool(
            incoming_zip and matched_zip and incoming_zip != matched_zip
        )
        features = match_features or match.get("match_features") or {}
        city_mismatch = bool(features.get("city_mismatch"))

        if house_number_neighbor_auto_net_new(incoming_address, candidate_address):
            return "net_new", combined, "house_number_neighbor", zip_mismatch

        delta = features.get("house_number_delta")
        if (
            delta is not None
            and delta >= HOUSE_NUMBER_FAR_DELTA_MIN
            and distance_m is not None
            and distance_m > HOUSE_NUMBER_FAR_DISTANCE_M
        ):
            return "net_new", combined, "house_number_far", zip_mismatch

        if address_score >= 100 and delta == 0:
            if city_mismatch and combined > CITY_MISMATCH_REVIEW_MIN_COMBINED:
                return (
                    "review",
                    max(combined, address_score),
                    "city_mismatch_high_confidence",
                    zip_mismatch,
                )
            return (
                "duplicate",
                max(combined, address_score),
                "address_exact_distance_override",
                zip_mismatch,
            )

        if distance_m is not None and distance_m < GEOCODER_COLLISION_MAX_M:
            jaccard = street_token_jaccard(incoming_address, candidate_address)
            if jaccard < GEOCODER_COLLISION_JACCARD_MIN:
                return "review", combined, "geocoder_collision_suspect", zip_mismatch
            if address_score < GEOCODER_COLLISION_MAX_ADDRESS:
                return "review", combined, "geocoder_collision", zip_mismatch

        if (
            distance_m is not None
            and address_score <= ADDRESS_FLOOR_SCORE
            and proximity_score_value > ADDRESS_FLOOR_PROXIMITY_MIN
            and incoming_zip
            and matched_zip
            and incoming_zip == matched_zip
        ):
            return "review", combined, "address_floor_proximity", zip_mismatch

        if (
            zip_mismatch
            and distance_m is not None
            and distance_m <= ZIP_MISMATCH_REVIEW_MAX_M
            and address_score < HIGH_ADDRESS_STRONG_MIN
        ):
            return "review", combined, "zip_mismatch_low_distance", zip_mismatch

        if address_score >= HIGH_ADDRESS_EXACT_MIN and (
            distance_m is None or distance_m <= max_review_m
        ):
            status = "duplicate"
            routing = "high_address_exact"
            if city_mismatch and combined > CITY_MISMATCH_REVIEW_MIN_COMBINED:
                return "review", max(combined, address_score), "city_mismatch_high_confidence", zip_mismatch
            return status, max(combined, address_score), routing, zip_mismatch

        if address_score >= HIGH_ADDRESS_STRONG_MIN and distance_m is not None:
            if distance_m <= search_radius_m:
                if city_mismatch and combined > CITY_MISMATCH_REVIEW_MIN_COMBINED:
                    return "review", max(combined, address_score), "city_mismatch_high_confidence", zip_mismatch
                return "duplicate", max(combined, address_score), "high_address_match", zip_mismatch
            if distance_m <= max_review_m:
                return "review", max(combined, address_score), "high_address_far", zip_mismatch
            return "net_new", combined, "high_address_beyond_2x_radius", zip_mismatch

        if not match.get("within_radius"):
            return "net_new", combined, None, zip_mismatch

        if distance_m is not None:
            if distance_m <= PROX_DUPLICATE_MAX_M and address_score >= PROX_DUPLICATE_MIN_ADDRESS:
                return "duplicate", combined, "proximity_duplicate", zip_mismatch
            if distance_m <= PROX_REVIEW_MAX_M and address_score >= PROX_REVIEW_MIN_ADDRESS:
                return "review", combined, "proximity_review", zip_mismatch
            if (
                distance_m <= PROX_REVIEW_EXTENDED_MAX_M
                and address_score >= PROX_REVIEW_EXTENDED_MIN_ADDRESS
            ):
                return "review", combined, "proximity_review_extended", zip_mismatch

        status = SiteResolver._status_from_combined_score(combined, address_score=address_score)
        if city_mismatch and combined > CITY_MISMATCH_REVIEW_MIN_COMBINED and status == "duplicate":
            return "review", combined, "city_mismatch_high_confidence", zip_mismatch
        return status, combined, None, zip_mismatch

    @staticmethod
    def _pick_best_match(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                item["address_score"],
                item["combined_score"],
                -(item.get("distance_m") or float("inf")),
            ),
        )

    @staticmethod
    def _pick_runner_up(
        candidates: list[dict[str, Any]],
        winner: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if winner is None or len(candidates) < 2:
            return None
        winner_id = winner["record"].get("Id")
        remaining = [
            item for item in candidates if item["record"].get("Id") != winner_id
        ]
        if not remaining:
            return None
        return SiteResolver._pick_best_match(remaining)

    @staticmethod
    def _status_from_combined_score(score: int, *, address_score: int) -> str:
        if score >= DUPLICATE_THRESHOLD:
            return "duplicate"
        if score >= REVIEW_THRESHOLD:
            return "review"
        if address_score < AUTO_REJECT_ADDRESS_SCORE:
            return "net_new"
        return "net_new"

    @staticmethod
    def is_potential_duplicate(
        *,
        status: str,
        match: dict[str, Any] | None,
    ) -> bool:
        if status != "net_new" or match is None:
            return False
        if not match.get("within_radius"):
            return False
        distance_m = match.get("distance_m")
        if distance_m is None or distance_m > POTENTIAL_DUPLICATE_MAX_DISTANCE_M:
            return False
        combined = match["combined_score"]
        if POTENTIAL_DUPLICATE_BORDERLINE_MIN <= combined <= POTENTIAL_DUPLICATE_BORDERLINE_MAX:
            return True
        return combined >= POTENTIAL_DUPLICATE_MIN_COMBINED

    @staticmethod
    def _build_resolution_detail(
        *,
        urbanicity: UrbanicityProfile,
        spatial_candidate_count: int,
        prefilter_count: int,
        match: dict[str, Any] | None,
        status: str,
        routing_reason: str | None,
        match_features: dict[str, Any] | None = None,
    ) -> str:
        radius = int(urbanicity.search_radius_m)
        pop = urbanicity.population
        pop_text = f"{pop:,}" if pop is not None else "unknown"
        if match is None:
            return (
                f"{urbanicity.tier} zip population={pop_text} radius={radius}m "
                f"prefilter={prefilter_count} spatial_candidates=0/{spatial_candidate_count}; "
                f"no in-radius Salesforce match; threshold_version={THRESHOLD_VERSION}"
            )

        distance_text = (
            f"{match['distance_m']:.0f}m"
            if match.get("distance_m") is not None
            else "no_coordinates"
        )
        coord_source = match.get("coordinate_source") or "missing"
        detail = (
            f"{urbanicity.tier} zip population={pop_text} radius={radius}m "
            f"prefilter={prefilter_count} spatial_candidates={spatial_candidate_count}; "
            f"address_score={match['address_score']} proximity_score={match['proximity_score']} "
            f"combined_score={match['combined_score']} distance={distance_text} "
            f"coord_source={coord_source}; threshold_version={THRESHOLD_VERSION}"
        )
        if match_features:
            delta = match_features.get("house_number_delta")
            if delta is not None:
                detail += f"; house_number_delta={delta}"
            if match_features.get("suffix_mismatch"):
                detail += "; suffix_mismatch=true"
            if match_features.get("city_mismatch"):
                detail += "; city_mismatch=true"
        if routing_reason:
            detail += f"; routing_reason={routing_reason}"
        detail += f"; status={status}"
        return detail

    @staticmethod
    def _build_gated_snapshots(
        gated: list[dict[str, Any]],
        *,
        incoming_address: str,
        incoming_zip: str | None,
        search_radius_m: float,
    ) -> list[dict[str, Any]]:
        ranked = sorted(
            gated,
            key=lambda item: (
                item.get("combined_score") or 0,
                item.get("address_score") or 0,
                -(item.get("distance_m") or float("inf")),
            ),
            reverse=True,
        )
        snapshots: list[dict[str, Any]] = []
        for item in ranked:
            candidate_address = normalize_sf_address(
                item["record"].get(SF_ADDRESS_FIELD) or item["record"].get("Name") or ""
            )
            matched_zip = SiteResolver._normalize_zip(item["record"].get(SF_ZIP_FIELD))
            _, _, routing_reason, _ = SiteResolver._resolve_match_status(
                item,
                search_radius_m=search_radius_m,
                incoming_zip=incoming_zip,
                matched_zip=matched_zip,
                incoming_address=incoming_address,
                candidate_address=candidate_address,
                match_features=item.get("match_features"),
            )
            snapshots.append(
                serialize_scored_candidate(item, routing_reason=routing_reason)
            )
        return snapshots

    @staticmethod
    def _build_top_candidates(
        snapshots: list[dict[str, Any]],
        *,
        prefilter_count: int,
    ) -> list[dict[str, Any]]:
        if prefilter_count < TOP_CANDIDATES_MIN_PREFILTER:
            return []
        return snapshots[:TOP_CANDIDATES_MAX]

    def resolve(
        self,
        record: dict[str, Any],
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run urbanicity-radius spatial gate + fuzzy dedupe and return status."""
        address = record["address"]
        scoring_address = normalize_for_scoring(address)
        incoming_lat = float(record["lat"])
        incoming_lng = float(record["lng"])
        incoming_zip = self._normalize_zip(record.get("zip_code"))
        incoming_city = extract_city_from_address(scoring_address)
        urbanicity = urbanicity_for_record(record)

        if is_intersection_address(address):
            pop_text = (
                f"{urbanicity.population:,}"
                if urbanicity.population is not None
                else "unknown"
            )
            resolution_detail = (
                f"{urbanicity.tier} zip population={pop_text} "
                f"radius={int(urbanicity.search_radius_m)}m; "
                f"routing_reason=intersection; status=net_new; "
                f"threshold_version={THRESHOLD_VERSION}"
            )
            return {
                "status": "net_new",
                "status_resolver": "net_new",
                "status_recommended": "net_new",
                "score": 0,
                "address_score": 0,
                "combined_score": 0,
                "proximity_score": None,
                "matched_distance_m": None,
                "matched_coordinate_source": None,
                "matched_record": None,
                "runner_up_record": None,
                "runner_up_score": None,
                "tie_breaker_close": False,
                "house_number_delta": None,
                "suffix_mismatch": False,
                "city_mismatch": False,
                "scoring_mode": SCORING_MODE_WEIGHTED,
                "top_candidates": "",
                "_gated_candidates": [],
                "candidate_count": 0,
                "spatial_candidate_count": 0,
                "prefilter_candidate_count": 0,
                "urbanicity": urbanicity.as_dict(),
                "resolution_detail": resolution_detail,
                "potential_duplicate": False,
                "routing_reason": "intersection",
                "proximity_rule": "intersection",
                "override_reason": None,
                "status_source": "resolver",
                "zip_mismatch": False,
                "threshold_version": THRESHOLD_VERSION,
                "distance_override_applied": False,
                "dataset_context": self._dataset_context,
            }

        pool = candidates if candidates is not None else self._candidate_cache
        if pool is None:
            raise RuntimeError(
                "Call prefetch() with the full dataset before resolve(). "
                "Dedupe uses one expanded bounding box from the dataset min/max "
                "lat/lng, not a per-site radius."
            )

        prefilter_max_m = max(urbanicity.search_radius_m, float(FUZZY_PREFILTER_MAX_M))
        filtered_pool = self._prefilter_candidates(
            pool,
            incoming_lat=incoming_lat,
            incoming_lng=incoming_lng,
            incoming_zip=incoming_zip,
            max_distance_m=prefilter_max_m,
        )

        scored = [
            self._score_candidate(
                scoring_address,
                incoming_lat,
                incoming_lng,
                sf_record,
                search_radius_m=urbanicity.search_radius_m,
                incoming_city=incoming_city,
            )
            for sf_record in filtered_pool
        ]
        in_radius = [item for item in scored if item["within_radius"]]
        spatial_candidate_count = len(in_radius)
        eligible = self._eligible_for_resolution(
            scored,
            search_radius_m=urbanicity.search_radius_m,
        )
        gated = self._gate_passing_candidates(eligible)
        gated_snapshots = self._build_gated_snapshots(
            gated,
            incoming_address=scoring_address,
            incoming_zip=incoming_zip,
            search_radius_m=urbanicity.search_radius_m,
        )
        match = self._pick_best_match(gated)
        runner_up = self._pick_runner_up(gated, match)
        match_features = (match or {}).get("match_features")
        matched_record = match["record"] if match else None
        city_mismatch = city_mismatch_for_review(
            incoming_city=incoming_city,
            matched_city=matched_record.get(SF_CITY_FIELD) if matched_record else None,
            incoming_address=scoring_address,
        )
        matched_zip = (
            self._normalize_zip(match["record"].get(SF_ZIP_FIELD)) if match else None
        )
        candidate_address = (
            normalize_sf_address(
                match["record"].get(SF_ADDRESS_FIELD) or match["record"].get("Name") or ""
            )
            if match
            else ""
        )

        if match is not None:
            status, score, routing_reason, zip_mismatch = self._resolve_match_status(
                match,
                search_radius_m=urbanicity.search_radius_m,
                incoming_zip=incoming_zip,
                matched_zip=matched_zip,
                incoming_address=scoring_address,
                candidate_address=candidate_address,
                match_features=match_features,
            )
            status_resolver = status
        else:
            score = 0
            status = "net_new"
            status_resolver = "net_new"
            routing_reason = None
            zip_mismatch = False

        distance_override_applied = routing_reason == "address_exact_distance_override"
        potential_duplicate = self.is_potential_duplicate(status=status, match=match)
        runner_up_record = runner_up["record"] if runner_up else None
        top_candidates = self._build_top_candidates(
            gated_snapshots,
            prefilter_count=len(filtered_pool),
        )
        scoring_mode = match.get("scoring_mode") if match else SCORING_MODE_WEIGHTED
        tie_breaker_close = False
        if match and runner_up:
            tie_breaker_close = abs(
                match["combined_score"] - runner_up["combined_score"]
            ) <= TIE_BREAKER_CLOSE_MAX_DELTA
        if tie_breaker_close and status == "duplicate":
            status = "review"
            routing_reason = "tie_breaker_close"

        status_recommended = status

        resolution_detail = self._build_resolution_detail(
            urbanicity=urbanicity,
            spatial_candidate_count=spatial_candidate_count,
            prefilter_count=len(filtered_pool),
            match=match,
            status=status,
            routing_reason=routing_reason,
            match_features=match_features,
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
                "    candidates : %d prefetched, %d within %dm prefilter, %d within radius",
                len(pool),
                len(filtered_pool),
                int(prefilter_max_m),
                spatial_candidate_count,
            )
            if match:
                dist = (
                    f"{match['distance_m']:.0f}m"
                    if match.get("distance_m") is not None
                    else "no_coordinates"
                )
                logger.info(
                    "    best match : %s | %s (coords=%s)",
                    matched.get("Id", "—"),
                    matched_addr[:80],
                    match.get("coordinate_source", "missing"),
                )
                logger.info(
                    "    scores     : address=%s proximity=%s combined=%s distance=%s",
                    match["address_score"],
                    match["proximity_score"],
                    match["combined_score"],
                    dist,
                )
            if potential_duplicate:
                logger.info("    flag       : potential_duplicate (manual calibration)")
            logger.info("    result     : %s — %s", status.upper(), resolution_detail)

        return {
            "status": status,
            "status_resolver": status_resolver,
            "status_recommended": status_recommended,
            "score": score,
            "address_score": match["address_score"] if match else 0,
            "combined_score": match["combined_score"] if match else 0,
            "proximity_score": match["proximity_score"] if match else None,
            "matched_distance_m": match["distance_m"] if match else None,
            "matched_coordinate_source": match.get("coordinate_source") if match else None,
            "matched_record": matched_record,
            "runner_up_record": runner_up_record,
            "runner_up_score": runner_up["combined_score"] if runner_up else None,
            "tie_breaker_close": tie_breaker_close,
            "house_number_delta": (match_features or {}).get("house_number_delta"),
            "suffix_mismatch": bool((match_features or {}).get("suffix_mismatch")),
            "city_mismatch": city_mismatch,
            "scoring_mode": scoring_mode,
            "top_candidates": top_candidates_json(top_candidates),
            "_gated_candidates": gated_snapshots,
            "candidate_count": len(pool),
            "spatial_candidate_count": spatial_candidate_count,
            "prefilter_candidate_count": len(filtered_pool),
            "urbanicity": urbanicity.as_dict(),
            "resolution_detail": resolution_detail,
            "potential_duplicate": potential_duplicate,
            "routing_reason": routing_reason,
            "proximity_rule": routing_reason,
            "override_reason": None,
            "status_source": "resolver",
            "zip_mismatch": zip_mismatch,
            "threshold_version": THRESHOLD_VERSION,
            "distance_override_applied": distance_override_applied,
            "dataset_context": self._dataset_context,
        }
