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
from enrichment.sf_ops import build_blank_site_type_query, build_update_payload


def _rooftop_ok(**overrides):
    base = {
        "site_type": "rooftop",
        "site_confidence": 0.9,
        "cell_equipment": True,
        "cell_equipment_confidence": 0.9,
        "cell_equipment_evidence": "North oblique shows sector panel antennas",
        "cell_gear_kind": "sector_panel",
        "cell_models_agree": True,
        "escalation_model": "claude",
        "asset_lat": 43.0005,
        "asset_lon": -89.0005,
        "asset_offset_m": 20.0,
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
            classified=_rooftop_ok(asset_lat="", asset_lon=""),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_needs_asset_box")

    def test_rooftop_naip_only_forbidden(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified=_rooftop_ok(nearmap_tier="naip_only", nearmap_views=""),
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["holdout_reason"], "rooftop_naip_only_forbidden")

    def test_tower_naip_only_forbidden(self):
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
        self.assertEqual(decision["holdout_reason"], "tower_naip_only_forbidden")

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
                "nearmap_tier": "vert_only",
                "nearmap_views": "Vert",
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
            classified=_rooftop_ok(site_confidence=0.7),
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
        self.assertIn("Site_Latitude__c != null", soql)
        self.assertIn("LLM_Classified__c = true", soql)


class PayloadTests(unittest.TestCase):
    def test_build_update_payload_sets_expected_fields(self):
        payload = build_update_payload(
            latitude=1.0,
            longitude=2.0,
            site_type="Rooftop",
            verified_site=True,
            verified_site_source="NearMap",
            llm_classified=True,
        )
        self.assertEqual(payload["Site_Latitude__c"], 1.0)
        self.assertEqual(payload["Site_Type__c"], "Rooftop")
        self.assertTrue(payload["Verified_Site__c"])
        self.assertEqual(payload["Verified_Site_Source__c"], "NearMap")
        self.assertTrue(payload["LLM_Classified__c"])


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
            {"LLM_Classified__c": False, "Test_Batch_Flag__c": True},
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
        self.assertEqual(entry["payload"], {"LLM_Classified__c": False})


if __name__ == "__main__":
    unittest.main()
