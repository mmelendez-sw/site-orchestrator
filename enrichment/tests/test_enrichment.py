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
    build_odbc_connection_string,
    fcc_coordinates,
    find_proximity_hit,
)
from enrichment.sf_ops import build_blank_site_type_query, build_update_payload


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


class FccCoordTests(unittest.TestCase):
    def test_decimal_precedes_calculated(self):
        coords = fcc_coordinates(
            {
                "Latitude_Decimal": 43.1,
                "Longitude_Decimal": -89.2,
                "Latitude_Calculated": 40.0,
                "Longitude_Calculated": -80.0,
            }
        )
        self.assertEqual(coords, (43.1, -89.2))

    def test_falls_back_to_calculated(self):
        coords = fcc_coordinates(
            {
                "Latitude_Decimal": None,
                "Longitude_Decimal": None,
                "Latitude_Calculated": 41.5,
                "Longitude_Calculated": -81.0,
            }
        )
        self.assertEqual(coords, (41.5, -81.0))


class ProximityTests(unittest.TestCase):
    def test_no_hit_outside_radius(self):
        # ~111 m north of origin point
        cursor = FakeCursor(
            fcc_rows=[
                {
                    "ID": 1,
                    "ASR_Number": "A",
                    "Latitude_Decimal": 43.001,
                    "Longitude_Decimal": -89.0,
                }
            ]
        )
        hit = find_proximity_hit(cursor, 43.0, -89.0, max_m=50)
        self.assertIsNone(hit)

    def test_fcc_hit_within_50m(self):
        # ~11 m north
        cursor = FakeCursor(
            fcc_rows=[
                {
                    "ID": 7,
                    "ASR_Number": "123",
                    "Latitude_Decimal": 43.0001,
                    "Longitude_Decimal": -89.0,
                    "Registration_Type": "Tower",
                }
            ]
        )
        hit = find_proximity_hit(cursor, 43.0, -89.0, max_m=50)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.source, MATCH_SOURCE_FCC)
        self.assertLessEqual(hit.distance_m, 50)
        self.assertEqual(hit.record_id, "7")

    def test_fcc_wins_equal_distance_tie(self):
        lat, lng = 43.0, -89.0
        cursor = FakeCursor(
            fcc_rows=[
                {
                    "ID": 1,
                    "ASR_Number": "FCC1",
                    "Latitude_Decimal": lat,
                    "Longitude_Decimal": lng,
                }
            ],
            ts_rows=[
                {
                    "operator_site_identifier": "TS1",
                    "latitude": lat,
                    "longitude": lng,
                    "asset_type": "monopole",
                }
            ],
        )
        hit = find_proximity_hit(cursor, lat, lng, max_m=50)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.source, MATCH_SOURCE_FCC)

    def test_towersource_when_fcc_absent(self):
        cursor = FakeCursor(
            ts_rows=[
                {
                    "operator_site_identifier": "TS9",
                    "latitude": 43.00005,
                    "longitude": -89.0,
                    "asset_type": "guyed",
                    "fcc_asr_number": "999",
                }
            ]
        )
        hit = find_proximity_hit(cursor, 43.0, -89.0, max_m=50)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.source, MATCH_SOURCE_TOWERSOURCE)
        self.assertEqual(hit.asset_type, "guyed")


class BucketTests(unittest.TestCase):
    def test_rooftop_without_cell_equipment_holdout(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={
                "site_type": "rooftop",
                "site_confidence": 0.9,
                "cell_equipment": False,
            },
            db_lat=43.0,
            db_lng=-89.0,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_ROOFTOP)
        self.assertEqual(decision["holdout_reason"], "rooftop_no_cell_equipment")
        self.assertEqual(decision["update_site_type"], "")

    def test_rooftop_with_cell_equipment_is_candidate(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={
                "site_type": "rooftop",
                "site_confidence": 0.85,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.9,
                "nearmap_tier": "vert_only",
                "nearmap_views": "Vert",
            },
            db_lat=43.01,
            db_lng=-89.01,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["holdout_reason"], "")
        self.assertEqual(decision["update_site_type"], "Rooftop")
        self.assertEqual(decision["update_lat"], 43.01)
        self.assertEqual(decision["update_lng"], -89.01)
        self.assertEqual(decision["update_verified_site"], True)
        self.assertEqual(decision["update_verified_site_source"], "FCC")

    def test_rooftop_low_cell_confidence_is_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "rooftop",
                "site_confidence": 0.9,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.4,
                "nearmap_tier": "vert_only",
                "nearmap_views": "Vert",
            },
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_ROOFTOP)
        self.assertEqual(decision["holdout_reason"], "rooftop_low_cell_confidence")

    def test_other_holdout(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={"site_type": "other", "site_confidence": 0.9},
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_OTHER)
        self.assertEqual(decision["holdout_reason"], "other")

    def test_tower_db_hit_is_candidate(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.85,
                "cell_equipment": True,
            },
            db_lat=43.01,
            db_lng=-89.01,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_lat"], 43.01)
        self.assertEqual(decision["update_lng"], -89.01)
        self.assertEqual(decision["update_site_type"], "Monopole")
        self.assertEqual(decision["update_verified_site"], True)
        self.assertEqual(decision["update_verified_site_source"], "FCC")

    def test_towersource_hit_uses_towersource_verified_source(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_TOWERSOURCE,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.85,
                "cell_equipment": True,
            },
            db_lat=43.01,
            db_lng=-89.01,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_verified_site"], True)
        self.assertEqual(
            decision["update_verified_site_source"],
            "TowerSource",
        )

    def test_distant_tower_asset_box_still_updates(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.8,
                "cell_equipment": True,
                "asset_lat": 45.132685,
                "asset_lon": -93.268497,
                "asset_offset_m": 149.0,
            },
            db_lat=45.131417,
            db_lng=-93.269111,
            sf_lat=45.131453,
            sf_lng=-93.269147,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_site_type"], "Monopole")
        self.assertEqual(decision["update_lat"], 45.131417)
        self.assertEqual(decision["update_lng"], -93.269111)

    def test_distant_rooftop_asset_box_is_held_out(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "rooftop",
                "site_confidence": 0.9,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.85,
                "asset_lat": 43.001,
                "asset_lon": -89.001,
                "asset_offset_m": 117.5,
                "nearmap_tier": "naip_only",
            },
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_ROOFTOP)
        self.assertIn("asset_offset_117.5m_exceeds_85m", decision["holdout_reason"])
        self.assertEqual(decision["update_site_type"], "")

    def test_rooftop_within_offset_leeway_snaps(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "rooftop",
                "site_confidence": 0.9,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.85,
                "asset_lat": 43.0007,
                "asset_lon": -89.0007,
                "asset_offset_m": 80.0,
                "nearmap_tier": "vert_only",
                "nearmap_views": "Vert",
            },
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_lat"], 43.0007)
        self.assertEqual(decision["update_lng"], -89.0007)
        self.assertEqual(decision["update_coord_source"], "naip_asset_box")

    def test_nearby_asset_box_still_updates(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={
                "site_type": "tower",
                "tower_subtype": "monopole",
                "site_confidence": 0.8,
                "cell_equipment": True,
                "asset_offset_m": 12.0,
            },
            db_lat=43.01,
            db_lng=-89.01,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_site_type"], "Monopole")

    def test_tower_naip_escalation_uses_asset_box(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "tower",
                "tower_subtype": "guyed",
                "site_confidence": 0.7,
                "asset_lat": 43.002,
                "asset_lon": -89.003,
                "nearmap_tier": "naip_only",
            },
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_lat"], 43.002)
        self.assertEqual(decision["update_lng"], -89.003)
        self.assertEqual(decision["update_coord_source"], "naip_asset_box")
        self.assertEqual(decision["update_verified_site"], True)
        self.assertEqual(decision["update_verified_site_source"], "NAIP")

    def test_nearmap_imagery_sets_verified_nearmap(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "rooftop",
                "site_confidence": 0.9,
                "cell_equipment": True,
                "cell_equipment_confidence": 0.85,
                "nearmap_tier": "vert_only",
                "nearmap_views": "Vert",
            },
            db_lat=None,
            db_lng=None,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(decision["update_site_type"], "Rooftop")
        self.assertEqual(decision["update_verified_site_source"], "NearMap")


class SoqlTests(unittest.TestCase):
    def test_blank_site_type_query(self):
        soql = build_blank_site_type_query()
        self.assertIn("Site_Type__c = null OR Site_Type__c = ''", soql)
        self.assertIn("Carrier_Leasing_Source__c LIKE '%NFL%'", soql)
        self.assertIn("LLM_Classified__c = true", soql)
        self.assertIn("Site_Latitude__c != null", soql)
        self.assertIn("Site_Longitude__c != null", soql)
        self.assertIn("Enhanced/Unreviewed", soql)
        self.assertNotIn("Outreach - Verified", soql)
        self.assertIn(
            "Stage__c NOT IN ('Working-Connected', 'Qualified (Converted)')",
            soql,
        )
        self.assertIn("Matthew Melendez", soql)
        self.assertIn("Site Acquisition Team", soql)
        self.assertNotIn("Site_Street__c LIKE", soql)

    def test_excluded_stages_remain_excluded_when_requested(self):
        soql = build_blank_site_type_query(
            stages=("Outreach", "Working-Connected", "Qualified (Converted)")
        )
        self.assertIn(
            "Stage__c IN ('Outreach', 'Working-Connected', 'Qualified (Converted)')",
            soql,
        )
        self.assertIn(
            "Stage__c NOT IN ('Working-Connected', 'Qualified (Converted)')",
            soql,
        )


class MssqlConnStringTests(unittest.TestCase):
    def test_defaults_to_token_auth_without_authentication_keyword(self):
        conn = build_odbc_connection_string(
            server="example.database.windows.net",
            database="db",
            authentication="",
        )
        self.assertNotIn("Authentication=", conn)
        self.assertIn("Server=tcp:example.database.windows.net,1433", conn)

    def test_rejects_dotnet_only_auth_value(self):
        # ActiveDirectoryDefault is not an ODBC value; it maps to token auth.
        conn = build_odbc_connection_string(
            server="example.database.windows.net",
            database="db",
            authentication="ActiveDirectoryDefault",
        )
        self.assertNotIn("Authentication=", conn)

    def test_rejects_unknown_auth_value(self):
        with self.assertRaises(ValueError):
            build_odbc_connection_string(
                server="example.database.windows.net",
                database="db",
                authentication="NotARealMode",
            )

    def test_rejects_interactive_auth(self):
        with self.assertRaises(ValueError):
            build_odbc_connection_string(
                server="example.database.windows.net",
                database="db",
                authentication="ActiveDirectoryInteractive",
            )

    def test_service_principal_requires_secrets(self):
        with self.assertRaises(ValueError):
            build_odbc_connection_string(
                server="example.database.windows.net",
                database="db",
                authentication="ActiveDirectoryServicePrincipal",
                uid="app-id",
                pwd="",
            )


class ProgressTimingTests(unittest.TestCase):
    def test_format_duration(self):
        from enrichment.progress import format_duration

        self.assertEqual(format_duration(3.2), "3.2s")
        self.assertEqual(format_duration(65), "1m05s")
        self.assertEqual(format_duration(3723), "1h02m03s")


class UpdatePayloadTests(unittest.TestCase):
    def test_llm_classified_checkbox(self):
        payload = build_update_payload(llm_classified=True)
        self.assertEqual(payload, {"LLM_Classified__c": True})

    def test_test_batch_flag(self):
        payload = build_update_payload(llm_classified=True, test_batch_flag=True)
        self.assertEqual(
            payload,
            {"LLM_Classified__c": True, "Test_Batch_Flag__c": True},
        )

    def test_payload_omits_blank(self):
        payload = build_update_payload(
            latitude=1.0,
            longitude=2.0,
            site_type="Monopole",
            verified_site=True,
            verified_site_source="FCC",
        )
        self.assertEqual(
            payload,
            {
                "Site_Latitude__c": 1.0,
                "Site_Longitude__c": 2.0,
                "Site_Type__c": "Monopole",
                "Verified_Site__c": True,
                "Verified_Site_Source__c": "FCC",
            },
        )


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
        self.assertIn("DUPLICATES_DETECTED", entry["error"])
        self.assertEqual(len(site.calls), 2)
        self.assertIn("Site_Type__c", site.calls[0][1])
        self.assertEqual(
            site.calls[1][1],
            {"LLM_Classified__c": False, "Test_Batch_Flag__c": True},
        )

    def test_non_duplicate_failure_does_not_fallback(self):
        from enrichment.sf_ops import apply_one_update

        class BoomSite:
            calls = []

            def update(self, record_id, payload):
                self.calls.append((record_id, dict(payload)))
                raise RuntimeError("REQUEST_LIMIT_EXCEEDED")

        site = BoomSite()
        entry = apply_one_update(
            _FakeClient(site),
            {
                "Id": "a0ZTEST000000002",
                "naip_site_type": "tower",
                "payload": {
                    "Site_Type__c": "Monopole",
                    "Site_Latitude__c": 1.0,
                    "Site_Longitude__c": 2.0,
                    "LLM_Classified__c": True,
                },
            },
            dry_run=False,
            verbose=False,
        )
        self.assertFalse(entry["success"])
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(len(site.calls), 1)

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
        self.assertEqual(site.calls[0][1], {"LLM_Classified__c": False})

    def test_llm_only_duplicate_does_not_retry(self):
        from enrichment.sf_ops import apply_one_update

        site = _FakeSiteSObject(fail_payloads_containing=["LLM_Classified__c"])
        entry = apply_one_update(
            _FakeClient(site),
            {
                "Id": "a0ZTEST000000003",
                "naip_site_type": "other",
                "payload": {"LLM_Classified__c": False},
            },
            dry_run=False,
            verbose=False,
        )
        self.assertFalse(entry["success"])
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(len(site.calls), 1)


if __name__ == "__main__":
    unittest.main()
