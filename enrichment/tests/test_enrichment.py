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
from enrichment.mssql import fcc_coordinates, find_proximity_hit
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
    def test_rooftop_holdout(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_FCC,
            classified={"site_type": "rooftop", "site_confidence": 0.9, "cell_equipment": True},
            db_lat=43.0,
            db_lng=-89.0,
            sf_lat=43.0,
            sf_lng=-89.0,
        )
        self.assertEqual(decision["bucket"], BUCKET_ROOFTOP)
        self.assertEqual(decision["holdout_reason"], "potential_rooftop")
        self.assertEqual(decision["update_site_type"], "")

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

    def test_tower_naip_escalation_uses_asset_box(self):
        decision = bucket_classification(
            match_source=MATCH_SOURCE_NONE,
            classified={
                "site_type": "tower",
                "tower_subtype": "guyed",
                "site_confidence": 0.7,
                "asset_lat": 43.002,
                "asset_lon": -89.003,
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
        self.assertEqual(decision["update_verified_site"], "")
        self.assertEqual(decision["update_verified_site_source"], "")


class SoqlTests(unittest.TestCase):
    def test_blank_site_type_query(self):
        soql = build_blank_site_type_query()
        self.assertIn("Site_Type__c = null OR Site_Type__c = ''", soql)
        self.assertIn("Enhanced/Unreviewed", soql)
        self.assertIn("Matthew Melendez", soql)


class UpdatePayloadTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
