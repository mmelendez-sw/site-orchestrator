"""Rate limiting and imagery caches (no network / paid APIs)."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


class RateLimiterTests(unittest.TestCase):
    def test_spaces_calls_across_threads(self):
        from classifier.llm import RateLimiter

        limiter = RateLimiter(per_minute=600)  # 0.1 s apart
        stamps: list[float] = []
        lock = threading.Lock()

        def call():
            limiter.acquire()
            with lock:
                stamps.append(time.monotonic())

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(gap >= 0.08 for gap in gaps), gaps)

    def test_cooldown_holds_every_caller(self):
        from classifier.llm import RateLimiter

        limiter = RateLimiter(per_minute=0)
        limiter.cooldown(0.15)
        t0 = time.monotonic()
        limiter.acquire()
        self.assertGreaterEqual(time.monotonic() - t0, 0.12)

    def test_gemini_429_cools_down_then_succeeds(self):
        from classifier import llm

        class Resp:
            text = '{"site_type": "tower", "site_confidence": 0.9}'

        class Models:
            calls = 0

            def generate_content(self, **_kwargs):
                Models.calls += 1
                if Models.calls == 1:
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
                return Resp()

        class Client:
            models = Models()

        with patch.object(llm, "_gemini_retry_wait_s", return_value=0.01), patch.object(
            llm, "GEMINI_LIMITER", llm.RateLimiter(0)
        ):
            out = llm.call_gemini_json(Client(), [], model="m", config=None, retries=2)
        self.assertEqual(out["site_type"], "tower")
        self.assertEqual(Models.calls, 2)


def _jpeg(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, format="JPEG")
    return buf.getvalue()


class NearmapTileCacheTests(unittest.TestCase):
    def test_second_fetch_is_served_from_disk(self):
        from classifier import imagery

        class Resp:
            status_code = 200
            ok = True
            content = _jpeg("red")

            def raise_for_status(self):
                pass

        calls = {"n": 0}

        def fake_get(_url):
            calls["n"] += 1
            return Resp()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"SITE_ORCHESTRATOR_DATA": tmp, "IMAGERY_CACHE": "1"}
        ), patch.object(imagery, "_nearmap_get", side_effect=fake_get):
            first = imagery._fetch_tile("Vert", 20, 1, 2, "2026-05-01")
            second = imagery._fetch_tile("Vert", 20, 1, 2, "2026-05-01")
            other_survey = imagery._fetch_tile("Vert", 20, 1, 2, "2026-08-01")
            unknown_date = imagery._fetch_tile("Vert", 20, 1, 2, None)
            self.assertTrue(
                (Path(tmp) / "cache" / "nearmap" / "2026-05-01" / "Vert" / "20" / "1_2.jpg").is_file()
            )
        self.assertEqual(first, (second[0], False))
        self.assertTrue(second[1])  # served from cache (not billed)
        self.assertEqual(other_survey, first)
        self.assertIsNotNone(unknown_date[0])
        # new survey and unknown date both refetch; the repeat does not
        self.assertEqual(calls["n"], 3)

    def test_cache_can_be_disabled(self):
        from classifier import imagery

        with patch.dict(os.environ, {"IMAGERY_CACHE": "0"}):
            self.assertIsNone(imagery._tile_cache_path("2026-05-01", "Vert", 20, 1, 2))


class NaipChipCacheTests(unittest.TestCase):
    def test_round_trip_recomputes_age(self):
        from classifier import imagery

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"SITE_ORCHESTRATOR_DATA": tmp, "IMAGERY_CACHE": "1"}
        ):
            paths = imagery._naip_cache_paths("m_4308901_ne_16_060_20240601", 43.0, -89.0, 300)
            img = Image.new("RGB", (8, 8), "green")
            meta = {"image_date": "2024-06-01", "image_age_years": 99.0, "naip_chip_m": 300}
            geo = {"crs": "EPSG:26916", "x_min": 0, "x_max": 300, "y_min": 0, "y_max": 300}
            imagery._store_naip_cache(paths, img, meta, geo)
            loaded = imagery._load_naip_cache(paths)
        self.assertIsNotNone(loaded)
        loaded_img, loaded_meta, loaded_geo = loaded
        self.assertEqual(loaded_img.size, (8, 8))
        self.assertEqual(loaded_geo, geo)
        self.assertLess(loaded_meta["image_age_years"], 99.0)


class NearmapSpendTests(unittest.TestCase):
    def _fake_get(self, calls):
        class Resp:
            status_code = 200
            ok = True
            content = _jpeg("red")

            def raise_for_status(self):
                pass

        def fake(url):
            calls.append(url)
            return Resp()

        return fake

    def test_meter_counts_billed_bytes_and_cache_hits_by_purpose(self):
        from classifier import imagery

        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"SITE_ORCHESTRATOR_DATA": tmp, "NEARMAP_API_KEY": "x"}
        ), patch.object(imagery, "_nearmap_get", side_effect=self._fake_get(calls)),                 patch.object(imagery, "nearmap_point_coverage", return_value=(True, "2026-05-01")),                 patch.object(imagery, "BUDGET", imagery.NearmapBudget(0, 90)):
            with imagery.nearmap_meter() as first:
                imagery.fetch_nearmap_views(43.0, -89.0, views=["Vert"])
            with imagery.nearmap_meter() as again:
                imagery.fetch_nearmap_views(43.0, -89.0, views=["Vert"], purpose="recenter")
        self.assertGreater(first.bytes, 0)
        self.assertEqual(first.tiles, len(calls))
        self.assertEqual(first.by_purpose, {"pack": first.bytes})
        self.assertEqual(again.bytes, 0)  # same tiles, all cache hits
        self.assertEqual(again.cache_hits, first.tiles)

    def test_budget_drops_optional_purchases_first_then_everything(self):
        from classifier import imagery

        budget = imagery.NearmapBudget(limit_mb=1, soft_pct=50)
        self.assertTrue(budget.allows("wide"))
        budget.seed(int(0.6 * 1024 * 1024))
        self.assertFalse(budget.allows("wide"))
        self.assertFalse(budget.allows("oblique_extra"))
        self.assertTrue(budget.allows("pack"))
        budget.add(1024 * 1024)
        self.assertFalse(budget.allows("pack"))
        self.assertTrue(imagery.NearmapBudget(0, 90).allows("wide"))  # 0 = off

    def test_budget_block_is_recorded_on_the_site_meter(self):
        from classifier import imagery

        full = imagery.NearmapBudget(limit_mb=1, soft_pct=90)
        full.seed(2 * 1024 * 1024)
        with patch.dict(os.environ, {"NEARMAP_API_KEY": "x"}), patch.object(
            imagery, "BUDGET", full
        ), patch.object(imagery, "nearmap_point_coverage", return_value=(True, "d")),                 patch.object(imagery, "_nearmap_get") as get:
            with imagery.nearmap_meter() as meter:
                views, _date = imagery.fetch_nearmap_views(43.0, -89.0)
        self.assertEqual(views, {})
        self.assertTrue(meter.budget_blocked)
        get.assert_not_called()


class StaggeredObliqueTests(unittest.TestCase):
    def _run(self, reply):
        from classifier import asset_classifier as ac

        fetched: list[list[str]] = []

        def fake_nearmap(lat, lon, chip_m=100, views=None, **_kw):
            fetched.append(list(views or []))
            return {v: Image.new("RGB", (64, 64)) for v in views}, "2026-05-01"

        def fake_pass(*_a, **kw):
            return dict(reply) if not kw.get("screen") else {
                "site_type": "rooftop", "site_confidence": 0.8}

        with patch.object(ac, "fetch_nearmap_views", side_effect=fake_nearmap),                 patch.object(ac, "_classify_pass", side_effect=fake_pass),                 patch.object(ac, "NEARMAP_API_KEY", "x"),                 patch.object(ac, "NEARMAP_STAGGER_OBLIQUES", True),                 patch.object(ac, "OBLIQUE_VIEWS", ["North", "East"]):
            res, nm_views, _d, tier, _v = ac.classify_with_tiers(
                43.0, -89.0, Image.new("RGB", (64, 64)), "gemini", {}, "p", "high",
                lambda nm: [(k, v) for k, v in nm.items()],
            )
        return fetched, nm_views, tier

    def test_locked_tower_skips_second_oblique(self):
        fetched, views, tier = self._run({
            "site_type": "tower", "site_confidence": 0.95, "cell_equipment": True})
        self.assertEqual(fetched, [["Vert"], ["North"]])
        self.assertEqual(sorted(views), ["North", "Vert"])
        self.assertEqual(tier, "full")

    def test_unsettled_rooftop_buys_remaining_obliques(self):
        fetched, views, tier = self._run({
            "site_type": "rooftop", "site_confidence": 0.8, "cell_equipment": False})
        self.assertEqual(fetched, [["Vert"], ["North"], ["East"]])
        self.assertEqual(sorted(views), ["East", "North", "Vert"])


if __name__ == "__main__":
    unittest.main()
