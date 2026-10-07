"""Supplemental (street-level / state ortho) labels never pass as Nearmap evidence."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from PIL import Image

from classifier.views import (
    is_oblique_label,
    is_state_ortho_label,
    is_street_level_label,
    is_top_down_label,
    trim_views_for_model,
)
from enrichment.bucketing import (
    _asset_view_is_nearmap_vert,
    _rooftop_localization_box_ok,
    _rooftop_oblique_imagery_ok,
    _street_confirm_source,
    bucket_classification,
)

STREET = "Street-level photo (Mapillary 2024-05, 38 m south of site, facing north)"
GSV = "Street-level photo (Google Street View 2025-03, 22 m west of site, facing east)"
ORTHO = "State orthoimagery top-down (NY 2024, ~15 cm)"


def _img():
    return Image.new("RGB", (32, 32))


class LabelTests(unittest.TestCase):
    def test_street_labels_are_not_obliques_or_top_down(self):
        self.assertTrue(is_street_level_label(STREET))
        self.assertFalse(is_oblique_label(STREET))
        self.assertFalse(is_top_down_label(STREET))

    def test_state_ortho_is_top_down_but_not_nearmap(self):
        self.assertTrue(is_state_ortho_label(ORTHO))
        self.assertTrue(is_top_down_label(ORTHO))
        self.assertFalse(is_oblique_label(ORTHO))
        self.assertFalse(_asset_view_is_nearmap_vert({"asset_view": ORTHO}))

    def test_nearmap_labels_unchanged(self):
        self.assertTrue(is_oblique_label("Nearmap oblique (North)"))
        self.assertTrue(_asset_view_is_nearmap_vert({"asset_view": "Nearmap top-down"}))


class TrimTests(unittest.TestCase):
    def _labels(self, views):
        return [label for label, _img in trim_views_for_model(views)]

    def test_top_down_preference_nearmap_then_ortho_then_naip(self):
        self.assertEqual(
            self._labels([("NAIP top-down", _img()), (ORTHO, _img()), ("Nearmap top-down", _img())]),
            ["Nearmap top-down"],
        )
        self.assertEqual(self._labels([("NAIP top-down", _img()), (ORTHO, _img())]), [ORTHO])
        self.assertEqual(self._labels([("NAIP top-down", _img())]), ["NAIP top-down"])

    def test_street_views_kept_and_capped(self):
        views = [("NAIP top-down", _img())] + [(f"{STREET} #{i}", _img()) for i in range(4)]
        labels = self._labels(views)
        self.assertEqual(labels[0], "NAIP top-down")
        self.assertEqual(sum(is_street_level_label(lbl) for lbl in labels), 2)


def _street_rooftop(**kw):
    res = {
        "site_type": "rooftop",
        "site_confidence": 0.9,
        "cell_equipment": True,
        "cell_equipment_confidence": 0.92,
        "cell_equipment_evidence": "sector panel antennas on the parapet",
        "cell_gear_kind": "sector_panel",
        "asset_box_2d": [400, 400, 520, 520],
        "asset_view": GSV,
        "dual_model_resolution": "agree_crop",
        "cell_models_agree": True,
        "escalation_model": "claude",
        "nearmap_tier": "naip_only",
        "nearmap_views": "",
    }
    res.update(kw)
    return res


class StreetConfirmGateTests(unittest.TestCase):
    def _bucket(self, classified):
        return bucket_classification(
            match_source="none", classified=classified,
            db_lat=None, db_lng=None, sf_lat=40.7, sf_lng=-74.0,
        )

    def test_off_by_default_street_box_cannot_write(self):
        with patch.dict(os.environ, {"SUPPLEMENTAL_CAN_CONFIRM": ""}):
            self.assertIsNone(_street_confirm_source(_street_rooftop()))
            self.assertFalse(_rooftop_oblique_imagery_ok(_street_rooftop()))
            out = self._bucket(_street_rooftop())
        self.assertNotEqual(out["bucket"], "potential_update")

    def test_opt_in_street_view_confirms_with_google_map_source(self):
        with patch.dict(os.environ, {"SUPPLEMENTAL_CAN_CONFIRM": "1"}):
            self.assertEqual(_street_confirm_source(_street_rooftop()), "streetview")
            self.assertTrue(_rooftop_localization_box_ok(_street_rooftop()))
            out = self._bucket(_street_rooftop())
        self.assertEqual(out["bucket"], "potential_update", out.get("holdout_reason"))
        self.assertEqual(out["update_verified_site_source"], "Google Map")
        self.assertEqual(out["update_coord_source"], "street_view_box_pin")

    def test_opt_in_mapillary_holds_out_until_picklist_exists(self):
        with patch.dict(os.environ, {"SUPPLEMENTAL_CAN_CONFIRM": "1"}):
            out = self._bucket(_street_rooftop(asset_view=STREET))
        self.assertEqual(out["holdout_reason"], "street_confirm_mapillary_needs_picklist")

    def test_opt_in_still_requires_dual_model_agreement(self):
        with patch.dict(os.environ, {"SUPPLEMENTAL_CAN_CONFIRM": "1"}):
            out = self._bucket(_street_rooftop(dual_model_resolution="claude_veto",
                                               cell_models_agree=False))
        self.assertNotEqual(out["bucket"], "potential_update")


if __name__ == "__main__":
    unittest.main()


class EvidenceOnlyIntegrationTests(unittest.TestCase):
    """SUPPLEMENTAL_IMAGERY without SUPPLEMENTAL_CAN_CONFIRM: street views join
    only model calls that already carry Nearmap, so the NAIP screen (and the
    buy-Nearmap decision) sees exactly what it saw before."""

    def _run(self, can_confirm: str):
        import tempfile
        from pathlib import Path

        from classifier import llm
        from classifier.sources import SupplementalView
        from enrichment import naip_classify
        from enrichment.tests import test_e2e_offline as e2e

        calls: list[list[str]] = []
        clients = e2e._FakeClients([], [])
        real_generate = clients["gemini"].models.generate_content

        def recording_generate(*, model, contents, config):
            calls.append([c for c in contents if isinstance(c, str) and c.startswith("View: ")])
            return real_generate(model=model, contents=contents, config=config)

        clients["gemini"].models.generate_content = recording_generate
        street = SupplementalView(label=GSV, image=_img(), source="streetview", captured=None, meta={})
        e2e._SITE_BY_THREAD["site"] = e2e.ROOF_ID
        env = {"NEARMAP_API_KEY": "x", "NEARMAP_TIERED": "1", "BIFURCATED_AI": "1",
               "GEMINI_ONLY": "0", "NAIP_ONLY": "0", "OSM_PREFILTER": "0",
               "SUPPLEMENTAL_CAN_CONFIRM": can_confirm}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, env), \
                patch("classifier.sources.enabled_sources", return_value=["streetview"]), \
                patch("classifier.sources.fetch_supplemental_views", return_value=[street]), \
                patch("classifier.imagery.fetch_chip", side_effect=e2e._fake_chip), \
                patch("classifier.imagery.fetch_nearmap_views", side_effect=e2e._fake_nearmap), \
                patch("classifier.asset_classifier.fetch_nearmap_views", side_effect=e2e._fake_nearmap), \
                patch("enrichment.osm_prefilter.lookup_osm_features", return_value={"ok": False}), \
                patch.object(naip_classify, "_build_clients", return_value=clients), \
                patch.object(llm, "GEMINI_LIMITER", llm.RateLimiter(0)), \
                patch.object(llm, "CLAUDE_LIMITER", llm.RateLimiter(0)):
            out = naip_classify.classify_site_imagery(
                site_id=e2e.ROOF_ID, lat=44.0, lon=-88.0, chip_dir=Path(tmp), verbose=False
            )
        return calls, out

    def test_evidence_only_keeps_naip_screen_unchanged(self):
        calls, out = self._run("0")
        naip_only = [c for c in calls if not any("Nearmap" in v for v in c)]
        with_nearmap = [c for c in calls if any("Nearmap" in v for v in c)]
        self.assertTrue(naip_only and with_nearmap)
        self.assertFalse(any(any("Street-level" in v for v in c) for c in naip_only))
        self.assertTrue(any(any("Street-level" in v for v in c) for c in with_nearmap))
        self.assertIn("Street-level", out["supplemental_views"])

    def test_can_confirm_adds_street_views_to_naip_screen(self):
        calls, _out = self._run("1")
        naip_only = [c for c in calls if not any("Nearmap" in v for v in c)]
        self.assertTrue(any(any("Street-level" in v for v in c) for c in naip_only))
