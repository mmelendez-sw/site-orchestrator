"""Registry matching: FCC Comprehensive, competitor portals, one structure per tower,
and registry-only runs that keep a site's existing Site_Type."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enrichment.constants import BUCKET_OTHER, BUCKET_POTENTIAL_UPDATE, PROXIMITY_MAX_M
from enrichment.cost_policy import auto_skip_classify_reason
from enrichment.mssql import competitor_coordinates, select_from_candidate_rows
from enrichment.sf_ops import build_row_payload
from salesforce.site_type_mapping import (
    site_type_from_db_asset_type,
    specific_site_type_from_db_asset_type,
)

PIN = (43.0, -89.0)
M = 1 / 111_320  # degrees of latitude per metre


def _fcc(i, north_m, asr, structure="TOWER"):
    return {"ID": i, "ASR_Number": asr, "Latitude_Decimal": PIN[0] + north_m * M,
            "Longitude_Decimal": PIN[1], "Registration_Type": structure}


def _ts(i, north_m, asr, asset_type="Generic Tower"):
    return {"operator_site_identifier": f"T{i}", "asset_type": asset_type, "fcc_asr_number": asr,
            "latitude": PIN[0] + north_m * M, "longitude": PIN[1]}


def _comp(key, north_m, asset_type="UNKNOWN", lng=-89.000012):
    return {"Unique_Key": key, "asset_type": asset_type,
            "latitude": round(PIN[0] + north_m * M, 6), "longitude": lng}


def _select(fcc=(), ts=(), comp=()):
    return select_from_candidate_rows(list(fcc), list(ts), *PIN, competitor_rows=list(comp))


class StructureGroupingTests(unittest.TestCase):
    def test_one_tower_in_fcc_and_towersource_is_one_unique_structure(self):
        # 2026-11-02 test: 5.4 m FCC + 5.4 m TowerSource used to read as an ambiguous pair.
        hit = _select(fcc=[_fcc(1, 5.4, "1281595")], ts=[_ts(1, 5.5, "1281595", "Guyed")])
        self.assertEqual(hit.candidate_count, 1)
        self.assertEqual(hit.source, "FCC")
        self.assertEqual(hit.sources, "FCC+TowerSource")
        self.assertEqual(hit.asset_type, "Guyed")  # TowerSource's type beats FCC's generic TOWER
        self.assertIsNotNone(auto_skip_classify_reason(hit, force=True))

    def test_rows_without_a_shared_asr_merge_only_across_registries(self):
        hit = _select(fcc=[_fcc(1, 20.8, "1025020")], ts=[_ts(1, 20.9, "", "Lattice / Self Standing")],
                      comp=[_comp("CC1", 20.8)])
        self.assertEqual(hit.candidate_count, 1)
        self.assertEqual(hit.sources, "FCC+TowerSource+Competitor")

    def test_two_registrations_of_one_registry_stay_two_structures(self):
        hit = _select(fcc=[_fcc(1, 12, "A"), _fcc(2, 16, "B")])
        self.assertEqual(hit.candidate_count, 2)
        # 4 m apart, nearest past 10 m, gap < 60 m: a collocation question, not written.
        self.assertIsNone(auto_skip_classify_reason(hit, force=True))

    def test_nearest_within_10_m_wins_a_cluster_in_db_only_runs(self):
        from enrichment.constants import DB_ONLY_ON_STRUCTURE_M

        hit = _select(fcc=[_fcc(1, 8, "A"), _fcc(2, 12, "B")], ts=[_ts(3, 24, "C")])
        self.assertEqual(hit.candidate_count, 3)
        self.assertEqual(hit.record_id, "1")
        self.assertIn("on-structure", auto_skip_classify_reason(
            hit, force=True, on_structure_m=DB_ONLY_ON_STRUCTURE_M) or "")
        # Imagery runs keep the 5 m rule: this cluster still gets imagery.
        self.assertIsNone(auto_skip_classify_reason(hit, force=True))

    def test_shared_asr_merges_even_when_coordinates_disagree(self):
        hit = _select(fcc=[_fcc(1, 3, "1226799", "TOWER")], ts=[_ts(1, 22, "1226799", "Guyed")])
        self.assertEqual(hit.candidate_count, 1)
        self.assertEqual(hit.asset_type, "Guyed")

    def test_two_real_towers_stay_ambiguous(self):
        hit = _select(fcc=[_fcc(1, 10.2, "1253800"), _fcc(2, 18.1, "1315896", "LTOWER")],
                      ts=[_ts(1, 9.9, "1253800"), _ts(2, 17.7, "1315896", "Lattice")])
        self.assertEqual(hit.candidate_count, 2)
        self.assertIsNone(auto_skip_classify_reason(hit, force=True))


class CompetitorSourceTests(unittest.TestCase):
    def test_imprecise_portal_points_are_ignored(self):
        self.assertIsNotNone(competitor_coordinates({"latitude": 41.638168, "longitude": -85.85849}))
        self.assertIsNotNone(competitor_coordinates({"latitude": 41.6382, "longitude": -85.8585}))
        self.assertIsNone(competitor_coordinates({"latitude": 41.638, "longitude": -85.858490}))
        self.assertIsNone(_select(comp=[{"Unique_Key": "x", "latitude": 43.0, "longitude": -89.0}]))

    def test_competitor_only_hit(self):
        hit = _select(comp=[_comp("GIS1", 4.1)])
        self.assertEqual(hit.sources, "Competitor")


class StructureTypeMappingTests(unittest.TestCase):
    def test_fcc_structure_codes(self):
        cases = {"MTOWER": "Monopole", "GTOWER": "Guyed Tower", "LTOWER": "Self Support / Lattice Tower",
                 "TANK": "Water Tower", "BANT": "Rooftop", "TREE": "Stealth", "Water Tank": "Water Tower",
                 "Generic Building": "Rooftop", "Monopine": "Stealth", "Self Supported": "Self Support / Lattice Tower"}
        for raw, expected in cases.items():
            self.assertEqual(site_type_from_db_asset_type(raw), expected, raw)

    def test_generic_labels_are_not_specific(self):
        for raw in ("TOWER", "Generic Tower", "UNKNOWN", "", "REG"):
            self.assertIsNone(specific_site_type_from_db_asset_type(raw), raw)
            self.assertEqual(site_type_from_db_asset_type(raw), "Self Support / Lattice Tower")


class _RegistryCursor:
    """Routes the registry queries by table; records the SQL it was sent."""

    def __init__(self, fcc=(), ts=(), comp=()):
        self.rows = {"fcc": list(fcc), "towersource": list(ts), "scrapedcompetitor": list(comp)}
        self.sql: list[str] = []
        self.description, self._pending = [], []

    def execute(self, sql, *params):
        self.sql.append(sql)
        text = sql.lower()
        key = next((k for k in ("scrapedcompetitor", "towersource", "fcc") if k in text), None)
        rows = self.rows.get(key, [])
        cols = sorted({c for r in rows for c in r})
        self.description = [(c,) for c in cols]
        self._pending = [tuple(r.get(c) for c in cols) for r in rows]

    def fetchall(self):
        return list(self._pending)


@patch.dict(os.environ, {"FCC_SOURCE": "comprehensive", "COMPETITOR_SOURCE": "1"})
class RegistryOnlyRunTests(unittest.TestCase):
    def _process(self, cursor, **site):
        from enrichment.pipeline import _process_site

        def boom(**_kwargs):
            raise AssertionError("classify must not run in db_only")

        with tempfile.TemporaryDirectory() as tmp:
            return _process_site(
                {"Id": "a0ZREG0000000001", "Site_Latitude__c": PIN[0], "Site_Longitude__c": PIN[1], **site},
                cursor=cursor, max_m=PROXIMITY_MAX_M, skip_classify=False, db_only=True,
                classify_fn=boom, chip_dir=Path(tmp), verbose=False,
            )

    def test_queries_comprehensive_without_dead_registrations_and_the_portals(self):
        cursor = _RegistryCursor()
        self._process(cursor)
        sql = " ".join(cursor.sql)
        self.assertIn("FCCComprehensiveTowerData", sql)
        self.assertIn("NOT IN ('Dismantled', 'Canceled', 'Terminated')", sql)
        self.assertIn("ScrapedCompetitorSites", sql)

    def test_blank_type_site_gets_the_registry_type_and_coordinates(self):
        cursor = _RegistryCursor(fcc=[_fcc(1, 4, "1298138", "MTOWER")], ts=[_ts(1, 4.1, "1298138", "Monopole")])
        row = self._process(cursor)
        self.assertEqual(row["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(row["update_site_type"], "Monopole")
        self.assertEqual(row["match_sources"], "FCC+TowerSource")
        self.assertNotEqual(row["update_lat"], "")

    def test_typed_site_keeps_its_type_pin_and_source(self):
        cursor = _RegistryCursor(fcc=[_fcc(1, 1.2, "1029253")], ts=[_ts(1, 1.4, "1029253")])
        row = self._process(cursor, Site_Type__c="Tower Land", Verified_Site_Source__c="Bing Map")
        self.assertEqual(row["bucket"], BUCKET_POTENTIAL_UPDATE)
        self.assertEqual(row["site_type_kept"], "Tower Land")
        self.assertEqual(row["db_site_type"], "Self Support / Lattice Tower")
        payload = build_row_payload(row, write_holdout=False)
        self.assertEqual(payload["Verified_Site__c"], True)
        for field in ("Site_Type__c", "Site_Latitude__c", "Site_Longitude__c", "Verified_Site_Source__c"):
            self.assertNotIn(field, payload)

    def test_typed_site_with_blank_source_gets_the_registry_source(self):
        cursor = _RegistryCursor(fcc=[_fcc(1, 1.2, "1029253")])
        row = self._process(cursor, Site_Type__c="Monopole")
        self.assertEqual(build_row_payload(row, write_holdout=False)["Verified_Site_Source__c"], "FCC")

    def test_near_miss_goes_to_review_not_salesforce(self):
        row = self._process(_RegistryCursor(fcc=[_fcc(1, 38, "1298138", "MTOWER")]),
                            Site_Type__c="Tower Land")
        self.assertEqual(row["bucket"], BUCKET_OTHER)
        self.assertIn("lone FCC structure 38 m away", row["registry_review"])
        self.assertEqual(row["db_site_type"], "Monopole")
        self.assertEqual(row["address_match"], "")  # registry row carries no address
        self.assertEqual(row.get("naip_check", ""), "")  # REGISTRY_NAIP_CHECK off by default
        from enrichment.pipeline import review_reason
        self.assertEqual(review_reason(row), row["registry_review"])

    def test_competitor_only_hit_needs_a_source_value(self):
        cursor = _RegistryCursor(comp=[_comp("GIS1", 4.1)])
        row = self._process(cursor)
        self.assertEqual(row["bucket"], BUCKET_OTHER)
        self.assertEqual(row["match_sources"], "Competitor")
        self.assertEqual(row["db_site_type"], "Self Support / Lattice Tower")
        self.assertEqual(row["registry_review"], "competitor portal only, 4 m away")
        with patch.dict(os.environ, {"COMPETITOR_VERIFIED_SOURCE": "Competitor Site"}):
            row = self._process(_RegistryCursor(comp=[_comp("GIS1", 4.1)]))
            self.assertEqual(row["bucket"], BUCKET_POTENTIAL_UPDATE)
            self.assertEqual(row["update_verified_site_source"], "Competitor Site")


class DensityAndReviewTests(unittest.TestCase):
    def test_density_tiers(self):
        from enrichment.cost_policy import density_tier

        self.assertEqual([density_tier(n) for n in (0, 3, 4, 12, 13)],
                         ["rural", "rural", "suburban", "suburban", "urban"])
        self.assertEqual(density_tier(None), "")

    def test_review_reasons(self):
        from enrichment.cost_policy import registry_review_reason

        cluster = _select(fcc=[_fcc(1, 14, "A"), _fcc(2, 18, "B")])
        self.assertIn("2 structures within 25 m", registry_review_reason(cluster))
        lone_40 = _select(fcc=[_fcc(1, 40, "A")])
        self.assertIn("lone FCC structure 40 m away, suburban", registry_review_reason(lone_40, "suburban"))
        lone_120 = _select(fcc=[_fcc(1, 120, "A")])
        self.assertEqual(registry_review_reason(lone_120, "suburban"), "")
        self.assertIn("rural", registry_review_reason(lone_120, "rural"))
        self.assertEqual(registry_review_reason(_select(fcc=[_fcc(1, 120, "A")]), "rural")[:4], "lone")
        self.assertEqual(registry_review_reason(None, "rural"), "")

    def test_density_counts_rows_from_sql(self):
        from enrichment.mssql import registry_density_bulk

        class _Cursor:
            def __init__(self):
                self.sql, self._rows = [], []

            def execute(self, sql, *params):
                self.sql.append((sql, params))
                self._rows = [("a", 7)] if "COUNT(*)" in sql else []

            def executemany(self, sql, rows):
                self.sql.append((sql, rows))

            def fetchall(self):
                return self._rows

        cur = _Cursor()
        counts = registry_density_bulk(cur, {"a": PIN, "b": (44.0, -90.0)}, radius_m=2000)
        self.assertEqual(counts, {"a": 7, "b": 0})
        count_sql, params = next(s for s in cur.sql if "COUNT(*)" in s[0])
        self.assertIn("FCCComprehensiveTowerData", count_sql)
        self.assertIn("TowerSourceASRTowers", count_sql)
        self.assertEqual(params, (4_000_000.0,))


class RegistryCheckTests(unittest.TestCase):
    def test_compare_addresses(self):
        from enrichment.registry_checks import compare_addresses

        cases = [
            (("1234 County Road 12", "53703", "1234 CR 12", ""), "number_and_street"),
            (("5512 N Oak Street", "", "OAK ST", ""), "street"),
            (("W5512 Highway 33", "", "HWY 33 & ELM", ""), "street"),
            (("100 Main St", "53703", "4 MILES N OF TOWN", "53703-1234"), "zip_only"),
            (("100 Main St", "53703", "22 Pine Rd", "60601"), "none"),
            (("", "53703", "22 Pine Rd", "53703"), ""),
        ]
        for args, expected in cases:
            self.assertEqual(compare_addresses(*args), expected, args)

    def test_best_address_across_merged_rows(self):
        from enrichment.registry_checks import address_match

        fcc = _fcc(1, 40, "1298138") | {"Structure_Street_Address": "N OF CR 12", "Structure_City": "X"}
        ts = _ts(1, 40.2, "1298138") | {"street1": "1234 County Rd 12", "city": "Madison", "postal_code": "53703"}
        hit = _select(fcc=[fcc], ts=[ts])
        got = address_match({"Site_Street__c": "1234 County Road 12", "Site_Zip_Code__c": "53703"}, hit)
        self.assertEqual(got, {"address_match": "number_and_street", "registry_address": "1234 County Rd 12, Madison"})

    def test_naip_check_reads_the_registry_point(self):
        from PIL import Image

        from enrichment.registry_checks import naip_registry_check

        geo = {"crs": "EPSG:32616", "x_min": 0, "x_max": 120, "y_min": 0, "y_max": 120, "chip_m": 120}
        with patch("classifier.imagery.fetch_chip", return_value=(Image.new("RGB", (200, 200)), {"image_date": "2023-06-01"}, geo)) as chip,              patch("classifier.asset_classifier._call_gemini_json",
                   return_value={"structure_at_ring": "tower", "confidence": 0.9, "evidence": "lattice shadow"}) as call:
            got = naip_registry_check(43.0, -89.0, client=object())
        chip.assert_called_once_with(43.0, -89.0, 120.0)
        self.assertEqual(call.call_count, 1)
        self.assertEqual((got["naip_check"], got["naip_check_date"]), ("tower", "2023-06-01"))

    def test_naip_check_never_raises(self):
        from enrichment.registry_checks import naip_registry_check

        with patch("classifier.imagery.fetch_chip", side_effect=RuntimeError("stac down")):
            self.assertEqual(naip_registry_check(43.0, -89.0, client=object())["naip_check"], "error")


class QueueFilterTests(unittest.TestCase):
    def test_never_attempted_and_site_type_exclude(self):
        from enrichment.sf_ops import build_blank_site_type_query, parse_not_attempted_since

        with patch.dict(os.environ, {"LLM_ATTEMPTED_FIELD": "LLM_Last_Attempted_Date__c"}):
            soql = build_blank_site_type_query(
                site_type="any",
                not_attempted_since=parse_not_attempted_since("never"),
                exclude_site_types=["Rooftop"],
            )
        self.assertIn("LLM_Last_Attempted_Date__c = null", soql)
        self.assertIn("(Site_Type__c = null OR Site_Type__c NOT IN ('Rooftop'))", soql)


if __name__ == "__main__":
    unittest.main()
