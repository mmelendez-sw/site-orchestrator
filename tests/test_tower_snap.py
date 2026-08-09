"""Unit tests for post-dedupe FCC/TowerSource coordinate snap."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from enrichment.constants import MATCH_SOURCE_FCC
from enrichment.mssql import ProximityHit
from tower_snap import apply_tower_db_snap, proximity_max_m


class ProximityMaxMTests(unittest.TestCase):
    def test_default_is_25(self):
        with patch.dict("os.environ", {}, clear=False):
            # Ensure unset
            import os

            os.environ.pop("ORCHESTRATOR_PROXIMITY_MAX_M", None)
            self.assertEqual(proximity_max_m(), 25.0)

    def test_env_override(self):
        with patch.dict("os.environ", {"ORCHESTRATOR_PROXIMITY_MAX_M": "30"}):
            self.assertEqual(proximity_max_m(), 30.0)


class ApplyTowerDbSnapTests(unittest.TestCase):
    def test_snaps_canonical_and_result_row(self):
        canonical = {
            "address": "100 Test St",
            "lat": 43.0,
            "lng": -89.0,
        }
        result_rows = [
            {"status": "net_new", "lat": 43.0, "lng": -89.0, "address": "100 Test St"}
        ]
        hit = ProximityHit(
            source=MATCH_SOURCE_FCC,
            distance_m=12.5,
            latitude=43.0001,
            longitude=-89.0001,
            record_id="7",
            asr_number="ASR1",
        )
        fake_conn = MagicMock()
        with patch("enrichment.mssql.find_proximity_hit", return_value=hit):
            stats = apply_tower_db_snap(
                [(0, canonical)],
                result_rows,
                max_m=25.0,
                sql_connection=fake_conn,
            )

        self.assertEqual(stats["snapped"], 1)
        self.assertEqual(canonical["lat"], 43.0001)
        self.assertEqual(canonical["lng"], -89.0001)
        self.assertEqual(canonical["geocode_lat"], 43.0)
        self.assertEqual(canonical["tower_snap_source"], MATCH_SOURCE_FCC)
        self.assertEqual(result_rows[0]["lat"], 43.0001)
        self.assertEqual(result_rows[0]["tower_snap_asr"], "ASR1")

    def test_no_hit_leaves_coords(self):
        canonical = {"address": "200 Test St", "lat": 43.0, "lng": -89.0}
        result_rows = [{"status": "net_new", "lat": 43.0, "lng": -89.0}]
        fake_conn = MagicMock()
        with patch("enrichment.mssql.find_proximity_hit", return_value=None):
            stats = apply_tower_db_snap(
                [(0, canonical)],
                result_rows,
                max_m=25.0,
                sql_connection=fake_conn,
            )
        self.assertEqual(stats["no_hit"], 1)
        self.assertEqual(canonical["lat"], 43.0)
        self.assertEqual(canonical["lng"], -89.0)

    def test_sql_connect_failure_is_non_fatal(self):
        canonical = {"address": "300 Test St", "lat": 43.0, "lng": -89.0}
        result_rows = [{"status": "net_new", "lat": 43.0, "lng": -89.0}]
        with patch(
            "enrichment.mssql.connect_mssql",
            side_effect=RuntimeError("no sql"),
        ):
            stats = apply_tower_db_snap(
                [(0, canonical)],
                result_rows,
                max_m=25.0,
            )
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(canonical["lat"], 43.0)


if __name__ == "__main__":
    unittest.main()
