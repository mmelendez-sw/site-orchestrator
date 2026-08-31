"""Unit tests for enrichment proximity + bucketing (no network / paid APIs)."""

from __future__ import annotations

import unittest

from enrichment.bucketing import bucket_classification
from enrichment.constants import (
    BUCKET_OTHER,
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    MATCH_SOURCE_FCC,
    MATCH_SOURCE_NONE,
    MATCH_SOURCE_TOWERSOURCE,
)
from enrichment.mssql import (
    fcc_coordinates,
    find_proximity_hit,
)
from enrichment.sf_ops import (
    build_blank_site_type_query,
    build_sites_by_ids_query,
    build_update_payload,
    parse_carrier_like,
    parse_metro_classification,
)


def _rooftop_ok(**overrides):
    base = {
        "site_type": "rooftop",
        "site_confidence": 0.9,
        "cell_equipment": True,
        "cell_equipment_confidence": 0.9,
        "cell_equipment_evidence": "North oblique shows sector panel antennas",
        "cell_gear_kind": "sector_panel",
        "cell_models_agree": True,
        "dual_model_resolution": "agree_crop",
        "escalation_model": "claude",
        "asset_lat": 43.0005,
        "asset_lon": -89.0005,
        "asset_offset_m": 20.0,
        "asset_box_2d": "[220, 310, 360, 420]",
        "asset_view": "Nearmap oblique (North)",
        "nearmap_tier": "full",
        "nearmap_views": "Vert,North,East,South,West",
    }
    base.update(overrides)
    return base


class FakeCursor:
    def __init__(self, fcc_rows=None, ts_rows=None):
        self.fcc_rows = fcc_rows or []
        self.ts_rows = ts_rows or []
        self.description = []
        self._pending = []

    def execute(self, sql, *params):
        text = " ".join(sql.split()).lower()
        if "fcctowerdata" in text:
            self.description = [
                ("ID",),
                ("ASR_Number",),
                ("Latitude_Decimal",),
                ("Longitude_Decimal",),
                ("Latitude_Calculated",),
                ("Longitude_Calculated",),
                ("Registration_Type",),
                ("Record_Type",),
                ("Entity_Name",),
            ]
            self._pending = [
                (
                    r.get("ID"),
                    r.get("ASR_Number"),
                    r.get("Latitude_Decimal"),
                    r.get("Longitude_Decimal"),
                    r.get("Latitude_Calculated"),
                    r.get("Longitude_Calculated"),
                    r.get("Registration_Type"),
                    r.get("Record_Type"),
                    r.get("Entity_Name"),
                )
                for r in self.fcc_rows
            ]
        else:
            self.description = [
                ("operator_site_identifier",),
                ("asset_name",),
                ("asset_type",),
                ("asset_category",),
                ("latitude",),
                ("longitude",),
                ("fcc_asr_number",),
                ("street1",),
                ("city",),
                ("state",),
                ("postal_code",),
            ]
            self._pending = [
                (
                    r.get("operator_site_identifier"),
                    r.get("asset_name"),
                    r.get("asset_type"),
                    r.get("asset_category"),
                    r.get("latitude"),
                    r.get("longitude"),
                    r.get("fcc_asr_number"),
                    r.get("street1"),
                    r.get("city"),
                    r.get("state"),
                    r.get("postal_code"),
                )
                for r in self.ts_rows
            ]

    def fetchall(self):
        return list(self._pending)


class MssqlHelperTests(unittest.TestCase):
    def test_fcc_prefers_decimal_over_calculated(self):
        coords = fcc_coordinates(
            {
                "Latitude_Decimal": 43.1,
                "Longitude_Decimal": -89.2,
                "Latitude_Calculated": 40.0,
                "Longitude_Calculated": -80.0,
            }
        )
        self.assertEqual(coords, (43.1, -89.2))

    def test_fcc_falls_back_to_calculated(self):
        coords = fcc_coordinates(
            {
                "Latitude_Decimal": None,
                "Longitude_Decimal": None,
                "Latitude_Calculated": 41.5,
                "Longitude_Calculated": -81.0,
            }
        )
        self.assertEqual(coords, (41.5, -81.0))

    def test_find_proximity_hit_returns_closest_within_radius(self):
        cursor = FakeCursor(
            fcc_rows=[
                {
                    "ID": 1,
                    "ASR_Number": "A",
                    "Latitude_Decimal": 43.001,
                    "Longitude_Decimal": -89.0,
                },
                {
                    "ID": 2,
                    "ASR_Number": "B",
                    "Latitude_Decimal": 43.0001,
                    "Longitude_Decimal": -89.0,
                },
            ]
        )
        hit = find_proximity_hit(cursor, 43.0, -89.0, max_m=200)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.asr_number, "B")
        self.assertEqual(hit.selection_reason, "confident_pin")

    def test_extended_unique_nearest_accepted(self):
        """Sunset-style: lone tower ~350 m away is OK; cluster is not."""
        from enrichment.mssql import ProximityHit, select_proximity_hit

        lone = select_proximity_hit(
            [
                ProximityHit(
                    source=MATCH_SOURCE_TOWERSOURCE,
                    distance_m=386.0,
                    latitude=36.06019,
                    longitude=-115.04861,
                    record_id="880978",
                    distance_to_pin_m=386.0,
                )
            ],
            confident_m=25,
            ambiguity_gap_m=75,
        )
        self.assertIsNotNone(lone)
        self.assertEqual(lone.selection_reason, "unique_nearest_extended")

        ambiguous = select_proximity_hit(
            [
                ProximityHit(
                    source=MATCH_SOURCE_TOWERSOURCE,
                    distance_m=200.0,
                    latitude=36.06,
                    longitude=-115.05,
                    record_id="1",
                    distance_to_pin_m=200.0,
                ),
                ProximityHit(
                    source=MATCH_SOURCE_FCC,
                    distance_m=220.0,
                    latitude=36.061,
                    longitude=-115.05,
                    record_id="2",
                    distance_to_pin_m=220.0,
                ),
            ],
            confident_m=25,
            ambiguity_gap_m=75,
        )
        self.assertIsNone(ambiguous)

    def test_address_affinity_prefers_tower_near_geocode(self):
        from enrichment.mssql import ProximityHit, select_proximity_hit

        hit = select_proximity_hit(
            [
                ProximityHit(
                    source=MATCH_SOURCE_TOWERSOURCE,
                    distance_m=40.0,
                    latitude=36.06,
                    longitude=-115.05,
                    record_id="near_addr",
                    distance_to_pin_m=120.0,
                    distance_to_address_m=40.0,
                ),
                ProximityHit(
                    source=MATCH_SOURCE_FCC,
                    distance_m=90.0,
                    latitude=36.061,
                    longitude=-115.051,
                    record_id="near_pin",
                    distance_to_pin_m=90.0,
                    distance_to_address_m=150.0,
                ),
            ],
            confident_m=25,
            ambiguity_gap_m=75,
            address_affinity_m=80,
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.record_id, "near_addr")
        self.assertEqual(hit.selection_reason, "address_affinity")


class BucketTests(unittest.TestCase):
    def test_rooftop_without_cell_equipment_holdout(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={
                "site_type": "rooftop",
                "site_confidence": 0.9,
                "cell_equipment": False,
                "nearmap_tier": "full",
                "nearmap_views": "Vert,North,East,South,West",
            },
            db_lat=43.0,
            db_lng=-89.0,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_ROOFTOP)
        self.assertEqual(decision["holdout_reason"], "rooftop_no_cell_equipment")

    def test_rooftop_with_cell_equipment_is_candidate(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified=_rooftop_ok(),
            db_lat=43.01,
            db_lng=-89.01,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_site_type"], "Rooftop")
        self.assertEqual(decision["update_verified_site_source"], "FCC")

    def test_rooftop_vert_only_is_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                nearmap_tier="vert_only",
                nearmap_views="Vert",
                site_confidence=0.9,
                cell_equipment_confidence=0.9,
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_ROOFTOP)
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_nearmap_obliques")

    def test_rooftop_needs_dual_model(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(cell_models_agree=False, escalation_model=""),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_dual_model_cell")

    def test_rooftop_needs_asset_box(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(asset_lat="", asset_lon="", asset_box_2d=""),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_asset_box")

    def test_rooftop_nearmap_box_pin_is_candidate(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                asset_lat="",
                asset_lon="",
                asset_box_2d="[220, 310, 360, 420]",
                asset_view="Nearmap oblique (North)",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_coord_source"], "nearmap_asset_box_pin")

    def test_rooftop_oblique_geocode_updates_coords(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                asset_lat=43.001,
                asset_lon=-89.002,
                asset_coord_source="nearmap_oblique_box",
                asset_offset_m=35.0,
                asset_box_2d="[220, 310, 360, 420]",
                asset_view="Nearmap oblique (North)",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_lat"], 43.001)
        self.assertEqual(decision["update_lng"], -89.002)
        self.assertEqual(decision["update_coord_source"], "nearmap_oblique_box")

    def test_rooftop_naip_box_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                asset_view="NAIP top-down",
                cell_equipment_evidence="South oblique shows sector panels",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_oblique_asset_box")

    def test_rooftop_dual_agree_allows_vert_box_and_078_conf(self):
        """Southeast Financial-style: crop agree + Vert box + conf 0.78."""
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                cell_equipment_confidence=0.78,
                dual_model_resolution="agree_crop",
                cell_models_agree=True,
                asset_view="Nearmap top-down",
                asset_box_2d="[476, 526, 632, 589]",
                asset_lat=25.7722,
                asset_lon=-80.1876,
                asset_offset_m=7.9,
                asset_coord_source="nearmap_vert_box",
                cell_equipment_evidence=(
                    "Nearmap top-down shows sector panels and microwave dishes "
                    "on a rooftop telecom frame"
                ),
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=25.77225,
            sf_lng=-80.18767,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_site_type"], "Rooftop")

    def test_imagery_only_bare_agree_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(dual_model_resolution="agree"),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(
            decision["holdout_reason"],
            "imagery_only_needs_crop_or_localize_agree",
        )

    def test_db_hit_bare_agree_uses_db_coords(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified=_rooftop_ok(dual_model_resolution="agree"),
            db_lat=43.01,
            db_lng=-89.01,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertTrue(str(decision["update_coord_source"]).startswith("db:"))
        self.assertEqual(decision["update_lat"], 43.01)

    def test_rooftop_gemini_strong_solo_is_held_out_for_sf(self):
        """Auto-apply airtight: Gemini solo skip is not enough for SF write."""
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                cell_equipment_confidence=0.92,
                dual_model_resolution="gemini_strong_solo",
                cell_models_agree=True,
                escalation_model="gemini_strong_solo",
                asset_view="Nearmap oblique (North)",
                asset_box_2d="[220, 310, 360, 420]",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_dual_model_cell")

    def test_rooftop_soft_keep_is_held_out_for_sf(self):
        """Auto-apply airtight: soft-keep Gemini never unlocks SF writes."""
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                cell_equipment_confidence=0.9,
                dual_model_resolution="soft_keep_gemini",
                cell_models_agree=True,
                asset_view="Nearmap oblique (North)",
                asset_box_2d="[220, 310, 360, 420]",
                cell_equipment_evidence="Gemini claimed sector panels; Claude veto soft-kept",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_dual_model_cell")

    def test_rooftop_soft_keep_does_not_relax_vert_box(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                cell_equipment_confidence=0.9,
                dual_model_resolution="soft_keep_gemini",
                cell_models_agree=True,
                asset_view="Nearmap top-down",
                asset_box_2d="[476, 526, 632, 589]",
                cell_equipment_evidence="Gemini claimed sector panels on Vert",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        # Soft-keep fails hard-agree before Vert-box localization is considered.
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_dual_model_cell")

    def test_rooftop_disagree_stays_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                cell_models_agree=False,
                dual_model_resolution="claude_veto",
                claude_cell_equipment=False,
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_dual_model_cell")


    def test_rooftop_view_evidence_mismatch_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                asset_view="Nearmap oblique (North)",
                cell_equipment_evidence="South oblique shows sector panel antennas",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_view_evidence_mismatch")

    def test_rooftop_unclear_gear_with_cues_ok(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(cell_gear_kind="unclear"),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)

    def test_rooftop_naip_uncertain_is_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(nearmap_tier="naip_only", nearmap_views=""),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_not_certain_on_naip")

    def test_rooftop_naip_certain_is_ready(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                site_confidence=0.97,
                cell_equipment_confidence=0.97,
                nearmap_tier="naip_only",
                nearmap_views="",
                asset_view="NAIP top-down",
                dual_model_resolution="",
                escalation_model="",
                cell_models_agree=False,
                cell_equipment_evidence="NAIP top-down shows sector panel antennas on a rooftop frame",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_site_type"], "Rooftop")
        self.assertEqual(decision["update_verified_site_source"], "NAIP")

    def test_rooftop_naip_hedged_evidence_is_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                site_confidence=0.97,
                cell_equipment_confidence=0.97,
                nearmap_tier="naip_only",
                nearmap_views="",
                asset_view="NAIP top-down",
                cell_equipment_evidence="Roof objects likely include sector panels",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_not_certain_on_naip")

    def test_rooftop_naip_unclear_gear_is_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(
                site_confidence=0.97,
                cell_equipment_confidence=0.97,
                cell_gear_kind="unclear",
                nearmap_tier="naip_only",
                nearmap_views="",
                asset_view="NAIP top-down",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_not_certain_on_naip")

    def test_imagery_only_naip_tower_is_ready(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.9,
                "cell_equipment": True,
                "nearmap_tier": "naip_only",
            },
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)

    def test_db_tower_naip_gemini_solo_is_ready(self):
        """DB-hit Gemini >= 0.9 may write from NAIP without Nearmap."""
        from enrichment.tests.golden_cases import _tower

        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified=_tower(
                dual_model_resolution="gemini_strong_solo",
                escalation_model="gemini_strong_solo",
                nearmap_tier="naip_only",
                nearmap_views="",
                asset_view="NAIP top-down",
            ),
            db_lat=36.0256,
            db_lng=-115.0853,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)

    def test_db_tower_naip_without_gemini_lock_still_forbidden(self):
        from enrichment.tests.golden_cases import _tower

        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified=_tower(
                dual_model_resolution="agree_crop",
                escalation_model="claude",
                nearmap_tier="naip_only",
                nearmap_views="",
            ),
            db_lat=36.0256,
            db_lng=-115.0853,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(decision["holdout_reason"], "tower_naip_only_forbidden")

    def test_db_tower_vert_gemini_solo_is_ready(self):
        """DB-hit Gemini >= 0.9 may write from Vert without obliques."""
        from enrichment.tests.golden_cases import _tower

        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified=_tower(
                dual_model_resolution="gemini_strong_solo",
                escalation_model="gemini_strong_solo",
                nearmap_tier="vert_only",
                nearmap_views="Vert",
                asset_view="Nearmap top-down",
            ),
            db_lat=36.0256,
            db_lng=-115.0853,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)

    def test_imagery_only_tower_vert_gemini_solo_is_ready(self):
        from enrichment.tests.golden_cases import _tower

        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_tower(
                dual_model_resolution="gemini_strong_solo",
                escalation_model="gemini_strong_solo",
                nearmap_tier="vert_only",
                nearmap_views="Vert",
                asset_view="Nearmap top-down",
            ),
            db_lat=None,
            db_lng=None,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)

    def test_imagery_only_tower_vert_only_is_ready(self):
        """Imagery-only Gemini tower at >= 0.9 may write without Nearmap obliques."""
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.9,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.8,
                "cell_equipment_evidence": "Nearmap top-down shows triangular platform",
                "nearmap_tier": "vert_only",
                "nearmap_views": "Vert",
                "asset_box_2d": "[188, 151, 584, 442]",
                "asset_view": "Nearmap top-down",
                "cell_models_agree": False,
            },
            db_lat=None,
            db_lng=None,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)

    def test_imagery_only_tower_below_gemini_lock_needs_dual_model(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.85,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.9,
                "cell_equipment_evidence": "North oblique shows sector panels on monopole",
                "nearmap_tier": "full",
                "nearmap_views": "Vert,North,East,South,West",
                "asset_box_2d": "[220, 310, 360, 420]",
                "asset_view": "Nearmap oblique (North)",
                "cell_models_agree": False,
                "escalation_model": "",
            },
            db_lat=None,
            db_lng=None,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(decision["holdout_reason"], "tower_needs_dual_model_cell")

    def test_tower_gemini_high_conf_solo_is_ready(self):
        """Gemini tower at >= 0.9 skips Claude and can write (DB or imagery)."""
        from enrichment.tests.golden_cases import _tower

        classified = _tower(
            dual_model_resolution="gemini_strong_solo",
            escalation_model="gemini_strong_solo",
        )
        db = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified=classified,
            db_lat=36.0256,
            db_lng=-115.0853,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(db["bucket"], BUCKET_POTENTIAL_UPDATE)
        imagery = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=classified,
            db_lat=None,
            db_lng=None,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(imagery["bucket"], BUCKET_POTENTIAL_UPDATE)

    def test_tower_gemini_solo_below_high_conf_held_out(self):
        from enrichment.tests.golden_cases import _tower

        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified=_tower(
                site_confidence=0.85,
                dual_model_resolution="gemini_strong_solo",
                escalation_model="gemini_strong_solo",
            ),
            db_lat=36.0256,
            db_lng=-115.0853,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(decision["holdout_reason"], "tower_needs_dual_model_cell")

    def test_tower_without_cell_equipment_is_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 1.0,
                "cell_equipment": False,
                "nearmap_tier": "full",
                "nearmap_views": "Vert,North,East,South,West",
            },
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "tower_no_cell_equipment")

    def test_tower_db_hit_is_candidate(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.85,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.9,
                "cell_equipment_evidence": "North oblique shows sector panels on monopole",
                "nearmap_tier": "full",
                "nearmap_views": "Vert,North,East,South,West",
                "asset_box_2d": "[220, 310, 360, 420]",
                "asset_view": "Nearmap oblique (North)",
                "cell_models_agree": True,
                "dual_model_resolution": "agree",
                "escalation_model": "claude",
            },
            db_lat=43.01,
            db_lng=-89.01,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_site_type"], "Monopole")

    def test_nearmap_imagery_sets_verified_nearmap(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_verified_site_source"], "NearMap")

    def test_distant_rooftop_asset_box_is_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(asset_offset_m=117.5),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_ROOFTOP)
        self.assertIn("asset_offset_117.5m_exceeds_85m", decision["holdout_reason"])

    def test_imagery_only_low_site_conf_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(site_confidence=0.69),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "low_confidence_imagery_only")


class SoqlTests(unittest.TestCase):
    def test_blank_site_type_query_contains_nfl_and_coords(self):
        soql = build_blank_site_type_query()
        self.assertIn("Carrier_Leasing_Source__c LIKE '%NFL%'", soql)
        self.assertIn("Metro_Classification__c = 'Major NFL Metro'", soql)
        self.assertIn("Site_Latitude__c != null", soql)
        self.assertIn("LLM_Classified__c = false", soql)
        self.assertIn("(LLM_Holdout__c = false OR LLM_Holdout__c = null)", soql)
        self.assertIn("Metro_Classification__c", soql.split(" FROM ")[0])

    def test_blank_site_type_query_can_select_already_classified(self):
        soql = build_blank_site_type_query(
            carrier_like="PermittingSites",
            llm_classified=True,
        )
        self.assertIn("LLM_Classified__c = true", soql)
        self.assertNotIn("LLM_Classified__c = false", soql)
        self.assertIn("Carrier_Leasing_Source__c LIKE '%PermittingSites%'", soql)

    def test_blank_site_type_query_can_omit_carrier(self):
        soql = build_blank_site_type_query(carrier_like=None)
        self.assertNotIn("Carrier_Leasing_Source__c LIKE", soql)
        self.assertIn("Metro_Classification__c = 'Major NFL Metro'", soql)
        self.assertIn("LLM_Classified__c = false", soql)
        self.assertIn("LLM_Holdout__c", soql)

    def test_blank_site_type_query_can_omit_metro(self):
        soql = build_blank_site_type_query(metro_classification=None)
        self.assertNotIn("Metro_Classification__c =", soql)
        self.assertIn("Carrier_Leasing_Source__c LIKE '%NFL%'", soql)

    def test_parse_carrier_like_env(self):
        self.assertEqual(parse_carrier_like(None), "NFL")
        self.assertEqual(parse_carrier_like(""), "NFL")
        self.assertEqual(parse_carrier_like("PermittingSites"), "PermittingSites")
        self.assertIsNone(parse_carrier_like("none"))
        self.assertIsNone(parse_carrier_like("ALL"))

    def test_parse_metro_classification_env(self):
        self.assertEqual(parse_metro_classification(None), "Major NFL Metro")
        self.assertEqual(parse_metro_classification(""), "Major NFL Metro")
        self.assertEqual(parse_metro_classification("Secondary Metro"), "Secondary Metro")
        self.assertIsNone(parse_metro_classification("none"))
        self.assertIsNone(parse_metro_classification("ALL"))

    def test_sites_by_ids_query(self):
        soql = build_sites_by_ids_query(["a0Z1", "a0Z2"])
        self.assertIn("Id IN ('a0Z1', 'a0Z2')", soql)
        self.assertNotIn("Carrier_Leasing_Source__c LIKE", soql)
        self.assertNotIn("LLM_Classified__c =", soql)

    def test_blank_site_type_query_states_filter(self):
        soql = build_blank_site_type_query(states=["CA", "fl", "NV", "MA"])
        self.assertIn("Site_State__c IN ('CA', 'FL', 'NV', 'MA')", soql)
        self.assertIn("Carrier_Leasing_Source__c LIKE '%NFL%'", soql)


class PayloadTests(unittest.TestCase):
    def test_build_update_payload_sets_expected_fields(self):
        payload = build_update_payload(
            latitude=1.0,
            longitude=2.0,
            site_type="Rooftop",
            verified_site=True,
            verified_site_source="NearMap",
            llm_classified=True,
            llm_holdout=False,
        )
        self.assertEqual(payload["Site_Latitude__c"], 1.0)
        self.assertEqual(payload["Site_Type__c"], "Rooftop")
        self.assertTrue(payload["Verified_Site__c"])
        self.assertEqual(payload["Verified_Site_Source__c"], "NearMap")
        self.assertTrue(payload["LLM_Classified__c"])
        self.assertFalse(payload["LLM_Holdout__c"])


class _FakeSiteSObject:
    def __init__(self, fail_payloads_containing=None):
        self.calls: list[tuple[str, dict]] = []
        self._fail_if = fail_payloads_containing or []

    def update(self, record_id, payload):
        self.calls.append((record_id, dict(payload)))
        for needle in self._fail_if:
            if needle in payload:
                raise RuntimeError(
                    "[{'errorCode': 'DUPLICATES_DETECTED', "
                    "'message': 'Same site address exists in the system.'}]"
                )
        return 204


class _FakeSF:
    def __init__(self, site_sobject):
        self.Site__c = site_sobject


class _FakeClient:
    def __init__(self, site_sobject):
        self.sf = _FakeSF(site_sobject)


class DuplicateFallbackTests(unittest.TestCase):
    def test_enrichment_duplicate_retries_llm_and_test_batch(self):
        from enrichment.sf_ops import apply_one_update

        site = _FakeSiteSObject(fail_payloads_containing=["Site_Type__c"])
        client = _FakeClient(site)
        row = {
            "Id": "a0ZTEST000000001",
            "naip_site_type": "tower",
            "payload": {
                "Site_Latitude__c": 1.0,
                "Site_Longitude__c": 2.0,
                "Site_Type__c": "Monopole",
                "Verified_Site__c": True,
                "Verified_Site_Source__c": "FCC",
                "LLM_Classified__c": True,
            },
        }
        entry = apply_one_update(client, row, dry_run=False, verbose=False)
        self.assertTrue(entry["success"])
        self.assertEqual(entry["status"], "updated_llm_after_duplicate")
        self.assertEqual(
            entry["payload"],
            {
                "LLM_Classified__c": False,
                "LLM_Holdout__c": True,
                "Test_Batch_Flag__c": True,
            },
        )
        self.assertEqual(len(site.calls), 2)

    def test_holdout_dequeues_with_llm_classified_false(self):
        from enrichment.sf_ops import apply_one_update

        site = _FakeSiteSObject()
        entry = apply_one_update(
            _FakeClient(site),
            {
                "Id": "a0ZTEST000000004",
                "naip_site_type": "rooftop",
                "update_lat": "",
                "update_lng": "",
                "update_site_type": "",
            },
            dry_run=False,
            verbose=False,
        )
        self.assertTrue(entry["success"])
        self.assertEqual(
            entry["payload"],
            {"LLM_Classified__c": False, "LLM_Holdout__c": True},
        )

    def test_enrichment_write_clears_holdout_flag(self):
        from enrichment.sf_ops import apply_one_update

        site = _FakeSiteSObject()
        entry = apply_one_update(
            _FakeClient(site),
            {
                "Id": "a0ZTEST000000005",
                "naip_site_type": "rooftop",
                "update_lat": 43.0,
                "update_lng": -89.0,
                "update_site_type": "Rooftop",
                "update_verified_site": True,
                "update_verified_site_source": "NearMap",
            },
            dry_run=False,
            verbose=False,
        )
        self.assertTrue(entry["success"])
        self.assertTrue(entry["payload"]["LLM_Classified__c"])
        self.assertFalse(entry["payload"]["LLM_Holdout__c"])
        self.assertEqual(entry["payload"]["Site_Type__c"], "Rooftop")


class GoldenRegressionTests(unittest.TestCase):
    def test_all_golden_cases(self):
        from enrichment.tests.golden_cases import GOLDEN_CASES

        for (
            name,
            match_source,
            classified,
            db_lat,
            db_lng,
            sf_lat,
            sf_lng,
            expect,
        ) in GOLDEN_CASES:
            with self.subTest(name=name):
                decision = bucket_classification(
                    match_source=match_source,
                    classified=classified,
                    db_lat=db_lat,
                    db_lng=db_lng,
                    sf_lat=sf_lat,
                    sf_lng=sf_lng,
                )
                if "bucket" in expect:
                    self.assertEqual(decision["bucket"], expect["bucket"], name)
                if "holdout_reason" in expect:
                    self.assertEqual(
                        decision["holdout_reason"], expect["holdout_reason"], name
                    )
                if "holdout_reason_contains" in expect:
                    self.assertIn(
                        expect["holdout_reason_contains"],
                        str(decision.get("holdout_reason") or ""),
                        name,
                    )
                if "coord_prefix" in expect:
                    self.assertTrue(
                        str(decision.get("update_coord_source") or "").startswith(
                            expect["coord_prefix"]
                        ),
                        name,
                    )


class CostPolicyTests(unittest.TestCase):
    def test_auto_skip_unique_close_hit(self):
        from enrichment.cost_policy import should_auto_skip_classify
        from enrichment.mssql import ProximityHit

        hit = ProximityHit(
            source="FCC",
            distance_m=12.0,
            latitude=43.0,
            longitude=-89.0,
            candidate_count=1,
            runner_up_gap_m=None,
        )
        self.assertTrue(should_auto_skip_classify(hit))

    def test_auto_skip_rejects_ambiguous_cluster(self):
        from enrichment.cost_policy import should_auto_skip_classify
        from enrichment.mssql import ProximityHit

        hit = ProximityHit(
            source="FCC",
            distance_m=10.0,
            latitude=43.0,
            longitude=-89.0,
            candidate_count=3,
            runner_up_gap_m=8.0,
        )
        self.assertFalse(should_auto_skip_classify(hit))

    def test_auto_skip_rejects_far_unique_hit(self):
        from enrichment.cost_policy import should_auto_skip_classify
        from enrichment.mssql import ProximityHit

        hit = ProximityHit(
            source="FCC",
            distance_m=80.0,
            latitude=43.0,
            longitude=-89.0,
            candidate_count=1,
        )
        self.assertFalse(should_auto_skip_classify(hit))

    def test_cluster_match_within_50m(self):
        from enrichment.cost_policy import find_cluster_match

        cache = [
            {
                "lat": 43.0,
                "lon": -89.0,
                "classified": {"site_type": "other", "site_confidence": 0.8},
            }
        ]
        # ~11 m north
        match = find_cluster_match(cache, 43.0001, -89.0)
        self.assertIsNotNone(match)
        self.assertIsNone(find_cluster_match(cache, 43.01, -89.0))

    def test_second_nearmap_skips_locked_empty(self):
        from enrichment.cost_policy import should_spend_second_nearmap

        unused = (39.0, -94.0)
        kwargs = dict(
            unused_point=unused,
            skip_paid_imagery=False,
            has_nearmap_key=True,
            site_type="other",
            site_confidence=0.9,
            nearmap_tier="full",
            rooftop_unlocked=False,
            cell_equipment=False,
        )
        self.assertFalse(should_spend_second_nearmap(**kwargs))
        kwargs["site_confidence"] = 0.8
        self.assertFalse(should_spend_second_nearmap(**kwargs))
        kwargs["cell_equipment"] = None
        self.assertFalse(should_spend_second_nearmap(**kwargs))
        kwargs["cell_equipment"] = True
        self.assertTrue(should_spend_second_nearmap(**kwargs))
        kwargs["cell_equipment"] = False
        kwargs["site_confidence"] = 0.9
        kwargs["nearmap_tier"] = "no_coverage"
        self.assertTrue(should_spend_second_nearmap(**kwargs))
        kwargs["nearmap_tier"] = "full"
        kwargs["site_type"] = "rooftop"
        kwargs["rooftop_unlocked"] = True
        self.assertTrue(should_spend_second_nearmap(**kwargs))

    def test_stamp_apply_status_before_metrics(self):
        from enrichment.pipeline import stamp_apply_status
        from enrichment.sf_ops import apply_queue_flags, build_update_payload

        enrich = apply_queue_flags(
            build_update_payload(
                latitude=39.0,
                longitude=-94.0,
                site_type="Rooftop",
                verified_site=True,
                verified_site_source="LLM Imagery",
            )
        )
        holdout = apply_queue_flags({})
        rows = [
            {"Id": "a", "sf_update_status": "pending", "sf_update_error": ""},
            {"Id": "b", "sf_update_status": "pending", "sf_update_error": ""},
            {"Id": "c", "sf_update_status": "pending", "sf_update_error": ""},
            {"Id": "d", "sf_update_status": "skipped", "sf_update_error": ""},
        ]
        stamp_apply_status(
            rows,
            [
                {"Id": "a", "success": True, "payload": enrich},
                {"Id": "b", "success": True, "payload": holdout},
                {
                    "Id": "c",
                    "success": False,
                    "payload": enrich,
                    "error": "DUPLICATES_DETECTED",
                },
            ],
        )
        self.assertEqual(rows[0]["sf_update_status"], "updated")
        self.assertEqual(rows[1]["sf_update_status"], "dequeued")
        self.assertEqual(rows[2]["sf_update_status"], "failed")
        self.assertEqual(rows[2]["sf_update_error"], "DUPLICATES_DETECTED")
        self.assertEqual(rows[3]["sf_update_status"], "skipped")

    def test_metrics_empty_to_rooftop_funnel(self):
        from enrichment.metrics import outcome_class, snapshot_run

        rows = [
            {
                "Id": "a",
                "naip_screen_site_type": "unclear",
                "naip_site_type": "rooftop",
                "naip_site_confidence": 0.8,
                "nearmap_tier": "full",
                "bucket": "potential_update",
                "update_site_type": "Rooftop",
                "escalation_model": "claude",
            },
            {
                "Id": "b",
                "naip_screen_site_type": "other",
                "naip_site_type": "other",
                "naip_site_confidence": 0.9,
                "nearmap_tier": "full",
                "bucket": "other_or_else",
                "holdout_reason": "other",
            },
            {
                "Id": "c",
                "naip_screen_site_type": "rooftop",
                "naip_site_type": "rooftop",
                "naip_site_confidence": 0.45,
                "nearmap_tier": "full",
                "bucket": "potential_rooftop",
                "holdout_reason": "low_confidence_imagery_only",
                "escalation_model": "claude",
            },
        ]
        self.assertEqual(outcome_class(rows[0]), "applied_rooftop")
        self.assertEqual(outcome_class(rows[1]), "holdout_empty_confirmed")
        self.assertEqual(outcome_class(rows[2]), "holdout_weak_rooftop")
        snap = snapshot_run(rows, run_id="test")
        self.assertEqual(snap["naip_empty_to_nearmap"], 2)
        self.assertEqual(snap["naip_empty_to_rooftop_apply"], 1)
        self.assertEqual(snap["empty_to_rooftop_apply_rate"], 0.5)
        self.assertEqual(snap["applied_rooftop"], 1)

    def test_metric_lines_match_ledger_keys(self):
        from enrichment.metrics import (
            RUN_METRIC_KEYS,
            format_metric_value,
            metric_lines,
        )

        self.assertEqual(format_metric_value("empty_to_rooftop_apply_rate", 0.333), "33.3%")
        self.assertEqual(format_metric_value("applied_rooftop", 5), "5")
        lines = metric_lines(
            {"sites": 10, "applied_rooftop": 2, "empty_to_rooftop_apply_rate": 0.2},
            RUN_METRIC_KEYS,
        )
        self.assertEqual(lines[0], "    sites: 10")
        self.assertIn("    applied_rooftop: 2", lines)
        self.assertIn("    empty_to_rooftop_apply_rate: 20.0%", lines)
        self.assertFalse(any("holdout_rooftop" in line for line in lines))
        self.assertFalse(any("gemini_sites" in line for line in lines))

    def test_metrics_sql_row_mapping(self):
        from enrichment.metrics_store import run_row, site_row

        run = run_row(
            {
                "run_id": "2026-08-28_150606_sf_enrichment",
                "recorded_at": "2026-08-28T19:17:07Z",
                "sites": 8,
                "applied_rooftop": 3,
                "empty_to_rooftop_apply_rate": 0.333,
            }
        )
        self.assertEqual(run[0], "2026-08-28_150606_sf_enrichment")
        self.assertEqual(run[2], 8)
        self.assertEqual(run[3], 3)
        site = site_row(
            {
                "run_id": "r1",
                "Id": "a0ZUq000004cJ72MAE",
                "Site_State__c": "CO",
                "Site_City__c": "DENVER",
                "Carrier_Leasing_Source__c": "AR_TMO_jan2025",
                "match_source": "none",
                "nearmap_ran": True,
                "empty_to_nearmap": 1,
                "outcome": "applied_rooftop",
            }
        )
        self.assertEqual(site[1], "a0ZUq000004cJ72MAE")
        self.assertEqual(site[3], "CO")
        self.assertEqual(site[6], "none")
        self.assertEqual(site[13], 1)
        self.assertEqual(site[24], "applied_rooftop")

    def test_site_state_from_address(self):
        from enrichment.metrics import site_state

        self.assertEqual(
            site_state({"address": "1598 Cleveland Place, DENVER, CO"}), "CO"
        )
        self.assertEqual(site_state({"Site_State__c": "ks"}), "KS")

    def test_metrics_ddl_loads_sql_file(self):
        from enrichment.metrics_ddl import ddl_statements

        batches = ddl_statements()
        joined = "\n".join(batches)
        self.assertTrue(any("CREATE TABLE dbo.EnrichmentRun" in b for b in batches))
        self.assertTrue(any("CREATE VIEW dbo.vEnrichmentKpisByState" in b for b in batches))
        self.assertIn("SiteState", joined)

    def test_metrics_rollup_last_id_wins(self):
        from enrichment.metrics import rollup_kpis

        kpis = rollup_kpis(
            [
                {"Id": "a", "outcome": "holdout_empty", "empty_to_nearmap": True},
                {
                    "Id": "a",
                    "outcome": "applied_rooftop",
                    "empty_to_nearmap": True,
                    "empty_to_rooftop_apply": True,
                    "nearmap_ran": True,
                },
                {"Id": "b", "outcome": "holdout_empty_confirmed", "nearmap_ran": True},
            ]
        )
        self.assertEqual(kpis["unique_sites"], 2)
        self.assertEqual(kpis["rooftop_sf_writes"], 1)
        self.assertEqual(kpis["holdout_empty_confirmed"], 1)
        self.assertEqual(kpis["naip_empty_to_rooftop_apply"], 1)

    def test_osm_empty_chip_and_tower_tags(self):
        from enrichment.osm_prefilter import (
            osm_suggests_empty_chip,
            parse_overpass_elements,
        )

        empty = parse_overpass_elements([])
        self.assertTrue(empty["ok"])
        self.assertTrue(osm_suggests_empty_chip(empty))

        tower = parse_overpass_elements(
            [
                {
                    "tags": {
                        "man_made": "mast",
                        "tower:type": "communication",
                    }
                }
            ]
        )
        self.assertTrue(tower["communication_tower"])
        self.assertFalse(osm_suggests_empty_chip(tower))
        self.assertFalse(osm_suggests_empty_chip({"ok": False}))


class SimpleClassifyStampTests(unittest.TestCase):
    def test_stamp_gemini_tower_solo_at_high_conf(self):
        from enrichment.classify_simple import stamp_gemini_tower_solo

        stamped = stamp_gemini_tower_solo(
            {
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.92,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.9,
            }
        )
        self.assertEqual(stamped["dual_model_resolution"], "gemini_strong_solo")
        self.assertTrue(stamped["cell_models_agree"])

    def test_rooftop_cell_certain_helper(self):
        from enrichment.bucketing import _rooftop_cell_certain

        certain = _rooftop_ok(
            site_confidence=0.97,
            cell_equipment_confidence=0.97,
            nearmap_tier="naip_only",
            nearmap_views="",
            asset_view="NAIP top-down",
            cell_equipment_evidence="NAIP top-down shows sector panel antennas on a rooftop frame",
        )
        self.assertTrue(_rooftop_cell_certain(certain))
        self.assertFalse(
            _rooftop_cell_certain(_rooftop_ok(site_confidence=0.9))
        )
        from enrichment.classify_simple import stamp_gemini_tower_solo

        roof = stamp_gemini_tower_solo(
            {
                "site_type": "rooftop",
                "site_confidence": 0.95,
                "cell_equipment": True,
            }
        )
        self.assertNotEqual(roof.get("dual_model_resolution"), "gemini_strong_solo")

        weak = stamp_gemini_tower_solo(
            {
                "site_type": "tower",
                "site_confidence": 0.7,
                "cell_equipment": True,
            }
        )
        self.assertNotEqual(weak.get("dual_model_resolution"), "gemini_strong_solo")

    def test_unstamped_naip_db_tower_at_high_conf_is_ready(self):
        """Barebones Gemini path may omit dual_model_resolution; 0.9 still writes."""
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.9,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.9,
                "nearmap_tier": "naip_only",
                "asset_view": "NAIP top-down",
            },
            db_lat=36.0256,
            db_lng=-115.0853,
            sf_lat=36.0255,
            sf_lng=-115.0852,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)


class PinAddressGeocodeTests(unittest.TestCase):
    def test_build_site_address(self):
        from enrichment.geo import build_site_address

        self.assertEqual(
            build_site_address(
                {
                    "Site_Street__c": "1598 Cleveland Place",
                    "Site_City__c": "DENVER",
                    "Site_State__c": "CO",
                    "Site_Zip_Code__c": "80202",
                }
            ),
            "1598 Cleveland Place, DENVER, CO 80202",
        )
        self.assertIsNone(build_site_address({"Site_City__c": "DENVER"}))

    def test_parse_census_payload_and_mismatch(self):
        from enrichment.geo import (
            parse_census_geocode_payload,
            pin_address_is_mismatch,
        )

        parsed = parse_census_geocode_payload(
            {
                "result": {
                    "addressMatches": [
                        {
                            "matchedAddress": "1598 CLEVELAND PL, DENVER, CO, 80202",
                            "coordinates": {"x": -104.9879, "y": 39.7417},
                        }
                    ]
                }
            }
        )
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed["lat"], 39.7417)
        self.assertAlmostEqual(parsed["lng"], -104.9879)
        self.assertEqual(parsed["source"], "census")
        self.assertFalse(pin_address_is_mismatch(20))
        self.assertTrue(pin_address_is_mismatch(75))
        self.assertTrue(pin_address_is_mismatch(180))
        self.assertIsNone(parse_census_geocode_payload({"result": {"addressMatches": []}}))

    def test_should_compare_rooftop_hosts(self):
        from enrichment.geo import should_compare_rooftop_hosts

        self.assertTrue(should_compare_rooftop_hosts(25))
        self.assertFalse(should_compare_rooftop_hosts(10))
        self.assertFalse(should_compare_rooftop_hosts(25, db_backed=True))
        self.assertFalse(should_compare_rooftop_hosts(None))

    def test_pick_classify_anchor_prefers_building_and_tower(self):
        from enrichment.geo import (
            mismatch_osm_is_decisive,
            pick_classify_anchor,
        )

        empty = {
            "ok": True,
            "has_building": False,
            "has_tower_or_mast": False,
            "communication_tower": False,
        }
        building = {
            "ok": True,
            "has_building": True,
            "has_tower_or_mast": False,
            "communication_tower": False,
        }
        tower = {
            "ok": True,
            "has_building": False,
            "has_tower_or_mast": True,
            "communication_tower": True,
        }
        pick, _reason = pick_classify_anchor(pin_osm=empty, address_osm=building)
        self.assertEqual(pick, "address")
        self.assertTrue(mismatch_osm_is_decisive(empty, building))
        pick, _reason = pick_classify_anchor(pin_osm=tower, address_osm=building)
        self.assertEqual(pick, "pin")
        pick, reason = pick_classify_anchor(pin_osm=building, address_osm=building)
        self.assertEqual(pick, "pin")
        self.assertIn("tie", reason)
        self.assertFalse(mismatch_osm_is_decisive(building, building))
        pick, _reason = pick_classify_anchor(
            pin_osm=empty,
            address_osm=empty,
            pin_naip={"site_type": "unclear", "site_confidence": 0.1},
            address_naip={"site_type": "rooftop", "site_confidence": 0.7},
        )
        self.assertEqual(pick, "address")


if __name__ == "__main__":
    unittest.main()
