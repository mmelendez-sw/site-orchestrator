"""End-to-end run with the real classifier and pipeline; only network edges are faked.

Gemini / Claude / NAIP / Nearmap / OSM / Salesforce / Azure SQL are stubbed
at their transport boundary, so this exercises prompt + schema building,
view trimming, tier gates, dual-model confirm, bucketing, Salesforce apply,
the detail CSV, and the metrics ledger exactly as a live run would.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from enrichment.constants import DETAIL_CSV

GEO = {"crs": "EPSG:26916", "x_min": 0.0, "x_max": 300.0, "y_min": 0.0, "y_max": 300.0,
       "chip_m": 300.0}
NAIP_META = {"image_date": "2025-06-01", "naip_year": "2025", "image_age_years": 1.0,
             "naip_chip_m": 300.0}

TOWER_ID = "a0ZE2ETOWER00001"
ROOF_ID = "a0ZE2EROOF000001"

REPLIES = {
    TOWER_ID: {
        "naip": {"site_type": "tower", "tower_subtype": "monopole", "site_confidence": 0.9,
                 "site_evidence": "monopole with long shadow", "cell_equipment": True,
                 "cell_equipment_confidence": 0.9, "cell_equipment_evidence": "sector antennas",
                 "cell_gear_kind": "sector_panel"},
        "nearmap": {"site_type": "tower", "tower_subtype": "monopole", "site_confidence": 0.93,
                    "site_evidence": "monopole", "cell_equipment": True,
                    "cell_equipment_confidence": 0.92,
                    "cell_equipment_evidence": "three sector antenna panels on the monopole",
                    "cell_gear_kind": "sector_panel", "asset_box_2d": [430, 440, 560, 560],
                    "asset_view": "Nearmap top-down"},
    },
    ROOF_ID: {
        "naip": {"site_type": "rooftop", "site_confidence": 0.8, "site_evidence": "flat roof",
                 "cell_equipment": True, "cell_equipment_confidence": 0.8,
                 "cell_equipment_evidence": "possible panels", "cell_gear_kind": "unclear"},
        "nearmap": {"site_type": "rooftop", "site_confidence": 0.85,
                    "site_evidence": "commercial roof with parapet",
                    "cell_equipment": True, "cell_equipment_confidence": 0.9,
                    "cell_equipment_evidence": "North oblique shows sector panel antennas on the parapet",
                    "cell_gear_kind": "sector_panel", "asset_box_2d": [420, 430, 520, 540],
                    "asset_view": "Nearmap oblique (North)"},
    },
}


class _GeminiModels:
    def __init__(self, log):
        self.log = log

    def generate_content(self, *, model, contents, config):
        labels = [c for c in contents if isinstance(c, str) and c.startswith("View: ")]
        images = [c for c in contents if not isinstance(c, str)]
        assert images, "every Gemini call carries images"
        assert config.response_schema is not None
        site = _current_site(contents)
        stage = "nearmap" if any("Nearmap" in label for label in labels) else "naip"
        self.log.append((site, stage, model))

        class Resp:
            text = json.dumps(REPLIES[site][stage])

        return Resp()


def _current_site(contents) -> str:
    return _SITE_BY_THREAD.get("site", TOWER_ID)


_SITE_BY_THREAD: dict[str, str] = {}


class _ClaudeMessages:
    def __init__(self, log):
        self.log = log

    def create(self, *, model, max_tokens, tools, tool_choice, messages):
        content = messages[0]["content"]
        assert any(block.get("type") == "image" for block in content)
        self.log.append((_SITE_BY_THREAD.get("site"), model))

        class Block:
            type = "tool_use"
            name = tool_choice["name"]
            input = {"site_type": "rooftop", "site_confidence": 0.9,
                     "site_evidence": "roof with antennas", "cell_equipment": True,
                     "cell_equipment_confidence": 0.9,
                     "cell_equipment_evidence": "sector panels visible in the crop",
                     "cell_gear_kind": "sector_panel"}

        class Resp:
            content = [Block()]

        return Resp()


class _FakeClients(dict):
    def __init__(self, gemini_log, claude_log):
        super().__init__()
        gemini = type("G", (), {})()
        gemini.models = _GeminiModels(gemini_log)
        claude = type("C", (), {})()
        claude.messages = _ClaudeMessages(claude_log)
        self["gemini"] = gemini
        self["claude"] = claude


class _SObject:
    def __init__(self):
        self.calls = []

    def update(self, record_id, payload):
        self.calls.append((record_id, dict(payload)))
        return 204


class _SF:
    def __init__(self):
        self.Site__c = _SObject()


class _Client:
    def __init__(self):
        self.sf = _SF()


class _EmptySql:
    class _Cursor:
        description = []

        def execute(self, *_a, **_k):
            self.description = [("ID",)] if "fcctowerdata" in " ".join(_a[0].split()).lower() else [("operator_site_identifier",)]

        def fetchall(self):
            return []

    def cursor(self):
        return self._Cursor()

    def close(self):
        pass


def _img(color: str, size=(512, 512)) -> Image.Image:
    return Image.new("RGB", size, color)


def _fake_chip(lat, lon, chip_m):
    return _img("gray"), dict(NAIP_META, naip_chip_m=chip_m), dict(GEO, chip_m=chip_m)


def _fake_nearmap(lat, lon, chip_m=100, views=None):
    names = views if views is not None else ["Vert", "North", "East"]
    return {name: _img("white" if name == "Vert" else "silver") for name in names}, "2026-05-01"


class OfflineEndToEndTests(unittest.TestCase):
    def test_tower_and_rooftop_sites_classify_bucket_and_apply(self):
        from classifier import llm
        from enrichment import naip_classify
        from enrichment.pipeline import run_enrichment
        import enrichment.metrics as metrics_mod

        gemini_log: list = []
        claude_log: list = []
        clients = _FakeClients(gemini_log, claude_log)
        sf = _Client()

        def classify(**kwargs):
            _SITE_BY_THREAD["site"] = kwargs["site_id"]
            return naip_classify.classify_site_imagery(**kwargs)

        sites = [
            {"Id": TOWER_ID, "Site_Latitude__c": 43.0, "Site_Longitude__c": -89.0,
             "Stage__c": "Outreach - Verified"},
            {"Id": ROOF_ID, "Site_Latitude__c": 44.0, "Site_Longitude__c": -88.0,
             "Stage__c": "Outreach - Verified"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "NEARMAP_API_KEY": "x",
                "METRICS_SQL": "0",
                "SITE_ORCHESTRATOR_DATA": tmp,
                "NEARMAP_TIERED": "1",
                "BIFURCATED_AI": "1",
                "GEMINI_ONLY": "0",
                "NAIP_ONLY": "0",
                "OSM_PREFILTER": "0",
            }
            old_dir = metrics_mod.metrics_dir
            metrics_mod.metrics_dir = lambda: root
            try:
                with patch.dict(os.environ, env), \
                        patch("classifier.imagery.fetch_chip", side_effect=_fake_chip), \
                        patch("classifier.imagery.fetch_nearmap_views", side_effect=_fake_nearmap), \
                        patch("classifier.asset_classifier.fetch_nearmap_views", side_effect=_fake_nearmap), \
                        patch("enrichment.osm_prefilter.lookup_osm_features",
                              return_value={"ok": False}), \
                        patch.object(naip_classify, "_build_clients", return_value=clients), \
                        patch.object(llm, "GEMINI_LIMITER", llm.RateLimiter(0)), \
                        patch.object(llm, "CLAUDE_LIMITER", llm.RateLimiter(0)):
                    summary = run_enrichment(
                        sf_client=sf,
                        sql_connection=_EmptySql(),
                        run_dir=root / "run",
                        sites=sites,
                        classify_fn=classify,
                        apply=True,
                        dequeue_holdouts=True,
                        verbose=False,
                        workers=1,
                    )
            finally:
                metrics_mod.metrics_dir = old_dir
            with (root / "run" / DETAIL_CSV).open(newline="", encoding="utf-8") as handle:
                detail = {row["Id"]: row for row in csv.DictReader(handle)}
            chips = sorted(p.name for p in (root / "run" / "chips").iterdir())

        tower, roof = detail[TOWER_ID], detail[ROOF_ID]
        # Tower: NAIP screen + Flash confirm + Nearmap pass, locked solo (no Claude).
        self.assertEqual(tower["bucket"], "potential_update")
        self.assertEqual(tower["update_site_type"], "Monopole")
        self.assertEqual(tower["nearmap_tier"], "full")
        self.assertEqual(tower["dual_model_resolution"], "gemini_strong_solo")
        self.assertEqual(tower["sf_update_status"], "updated")
        # Rooftop: Nearmap obliques + Claude crop agree → Rooftop write.
        self.assertEqual(roof["bucket"], "potential_update", roof["holdout_reason"])
        self.assertEqual(roof["update_site_type"], "Rooftop")
        self.assertEqual(roof["dual_model_resolution"], "agree_crop")
        self.assertEqual(roof["update_verified_site_source"], "NearMap")
        self.assertEqual(roof["sf_update_status"], "updated")
        self.assertTrue(any(site == ROOF_ID for site, _model in claude_log))
        self.assertFalse(any(site == TOWER_ID for site, _model in claude_log))
        stages = {(site, stage) for site, stage, _m in gemini_log}
        self.assertIn((TOWER_ID, "naip"), stages)
        self.assertIn((ROOF_ID, "nearmap"), stages)
        self.assertIn(f"{ROOF_ID}_nearmap_north.jpg", chips)
        written = {call[0]: call[1] for call in sf.sf.Site__c.calls}
        self.assertEqual(written[ROOF_ID]["Site_Type__c"], "Rooftop")
        self.assertTrue(written[TOWER_ID]["LLM_Classified__c"])
        self.assertEqual(summary["kpis"]["unique_sites"], 2)
        self.assertEqual(summary["kpis"]["written_sites"], 2)


if __name__ == "__main__":
    unittest.main()
