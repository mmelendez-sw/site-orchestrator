"""Supplemental imagery sources (fully offline: the HTTP session is faked)."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from classifier import sources
from classifier.sources import base, mapillary
from classifier.views import is_oblique_label, is_top_down_label, trim_views_for_model

SITE_LAT, SITE_LON = 40.0, -74.0
MAPILLARY_TOKEN = "MLY|secret-mapillary-token-123"

SOURCE_ENV = (
    "SUPPLEMENTAL_IMAGERY",
    "MAPILLARY_ACCESS_TOKEN", "MAPILLARY_RADIUS_M", "MAPILLARY_MAX_HEADING_DIFF",
    "MAPILLARY_MAX_VIEWS", "MAPILLARY_SEARCH_CACHE_DAYS",
    "SUPPLEMENTAL_TIMEOUT_S", "IMAGERY_CACHE", "SITE_ORCHESTRATOR_DATA",
    "MAPILLARY_CROP", "MAPILLARY_HFOV", "MAPILLARY_CROP_WIDTH", "MAPILLARY_CROP_TOP",
    "STREET_CONFIRM_MAX_AGE_YEARS",
)


def textured_image(size: int = 64) -> Image.Image:
    grad = Image.linear_gradient("L").resize((size, size))
    return Image.merge("RGB", (grad, grad.rotate(90), grad.rotate(180)))


def jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


class FakeResp:
    def __init__(self, status: int = 200, content: bytes = b"", ctype: str = "image/jpeg",
                 payload=None):
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": ctype}
        self._payload = payload

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def get(self, url, params=None, headers=None, timeout=None):
        with self._lock:
            self.calls.append({"url": url, "params": dict(params or {}),
                               "headers": dict(headers or {}), "timeout": timeout})
        return self.handler(url, dict(params or {}), dict(headers or {}))


@contextmanager
def clean_env(**values):
    with patch.dict(os.environ, {}, clear=False):
        for key in SOURCE_ENV:
            os.environ.pop(key, None)
        os.environ.update({k: str(v) for k, v in values.items()})
        yield


class SourcesTestCase(unittest.TestCase):
    def setUp(self):
        base._reset_log_once()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def env(self, **values):
        values.setdefault("SITE_ORCHESTRATOR_DATA", str(self.tmp / "data"))
        return clean_env(**values)

    def fake(self, handler) -> FakeSession:
        session = FakeSession(handler)
        patcher = patch.object(base, "session", lambda: session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session


# ------------------------------- geometry ----------------------------------


class GeometryTests(SourcesTestCase):
    def test_haversine_and_bearing(self):
        self.assertAlmostEqual(base.haversine_m(0, 0, 1, 0), 111_195, delta=50)
        self.assertAlmostEqual(base.initial_bearing_deg(0, 0, 0, 1), 90.0, places=6)
        self.assertAlmostEqual(base.initial_bearing_deg(0, 0, 1, 0), 0.0, places=6)
        self.assertAlmostEqual(base.initial_bearing_deg(0, 0, -1, 0), 180.0, places=6)

    def test_destination_round_trip(self):
        lat, lon = base.destination_point(SITE_LAT, SITE_LON, 40.0, 135.0)
        self.assertAlmostEqual(base.haversine_m(SITE_LAT, SITE_LON, lat, lon), 40.0, delta=0.01)
        self.assertAlmostEqual(base.initial_bearing_deg(SITE_LAT, SITE_LON, lat, lon), 135.0, delta=0.01)

    def test_angle_diff_and_compass(self):
        self.assertEqual(base.angle_diff_deg(350, 10), 20)
        self.assertEqual(base.angle_diff_deg(10, 190), 180)
        self.assertEqual(base.compass_abbrev(0), "N")
        self.assertEqual(base.compass_abbrev(359), "N")
        self.assertEqual(base.compass_abbrev(100), "E")
        self.assertEqual(base.compass_abbrev(225), "SW")

    def test_iso_date_from_epoch_ms(self):
        self.assertEqual(base.iso_date_from_epoch_ms(1641206630491), "2022-01-03")
        self.assertIsNone(base.iso_date_from_epoch_ms(None))



# ----------------------------- enabled_sources ------------------------------


class EnabledSourcesTests(SourcesTestCase):
    def test_unset_and_none(self):
        with self.env():
            self.assertEqual(sources.enabled_sources(), [])
        with self.env(SUPPLEMENTAL_IMAGERY="none", MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
            self.assertEqual(sources.enabled_sources(), [])

    def test_unknown_ignored_and_missing_credentials_dropped(self):
        with self.env(SUPPLEMENTAL_IMAGERY="mapillary,bogus,streetview",
                      MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
            with self.assertLogs("classifier.sources", level="WARNING") as logs:
                self.assertEqual(sources.enabled_sources(), ["mapillary"])
        text = "\n".join(logs.output)
        self.assertIn("bogus", text)
        self.assertIn("streetview", text)
        self.assertNotIn(MAPILLARY_TOKEN, text)

    def test_logged_once(self):
        with self.env(SUPPLEMENTAL_IMAGERY="bogus"):
            with self.assertLogs("classifier.sources", level="WARNING") as logs:
                sources.enabled_sources()
                sources.enabled_sources()
                base.logger.warning("sentinel")
        self.assertEqual(sum("bogus" in line for line in logs.output), 1)

    def test_names_normalized_and_deduped(self):
        with self.env(SUPPLEMENTAL_IMAGERY=" Mapillary , MAPILLARY",
                      MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
            self.assertEqual(sources.enabled_sources(), ["mapillary"])


# --------------------------------- Mapillary ---------------------------------


def mly_image(image_id: str, dist: float, approach: float, *, heading_offset: float = 0.0,
              captured_ms: int = 1_717_200_000_000, pano: bool = False, computed: bool = True):
    """Image ``dist`` m from the site on bearing ``approach`` (site -> camera),
    pointing at the site (+ ``heading_offset``)."""
    lat, lon = base.destination_point(SITE_LAT, SITE_LON, dist, approach)
    heading = ((approach + 180.0) + heading_offset) % 360.0
    image = {
        "id": image_id,
        "thumb_2048_url": f"https://cdn.invalid/{image_id}.jpg",
        "captured_at": captured_ms,
        "is_pano": pano,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }
    if computed:
        image["computed_geometry"] = {"type": "Point", "coordinates": [lon, lat]}
        image["computed_compass_angle"] = heading
        image["compass_angle"] = (heading + 90) % 360  # ignored when computed exists
    else:
        image["compass_angle"] = heading
    return image


MLY_IMAGES = [
    mly_image("best_s", 30, 180),                                   # S, facing N
    mly_image("far", 140, 0),                                       # beyond 100 m
    mly_image("close", 5, 90),                                      # under 8 m
    mly_image("misaligned", 25, 270, heading_offset=80),            # looks away
    mly_image("pano", 20, 45, pano=True),
    mly_image("older_s", 32, 190, captured_ms=1_600_000_000_000),   # same side as best
    mly_image("east", 40, 90, heading_offset=5),                    # different side
    mly_image("raw_compass", 35, 300, computed=False, captured_ms=1_500_000_000_000),
]


class MapillaryCropTests(SourcesTestCase):
    def test_crop_follows_site_bearing(self):
        from PIL import Image

        img = Image.new("RGB", (2000, 1500))
        with self.env():
            ahead = mapillary.crop_toward_site(img, 90.0, 90.0)
            right = mapillary.crop_toward_site(img, 90.0, 120.0)   # site 30 deg right
            left = mapillary.crop_toward_site(img, 350.0, 330.0)   # site 20 deg left, across north
        self.assertEqual(ahead.size, (1100, 1050))
        self.assertEqual(right.size, (1100, 1050))
        self.assertEqual(left.size, (1100, 1050))

    def test_old_photos_rank_after_recent(self):
        now_ms = time.time() * 1000
        imgs = [mly_image("old_aligned", 30, 180, captured_ms=now_ms - 8 * 365 * 86400e3),
                mly_image("new_offset", 30, 180, heading_offset=25, captured_ms=now_ms - 365 * 86400e3)]
        with self.env():
            ids = [c["id"] for c in mapillary.candidates(SITE_LAT, SITE_LON, imgs)]
        self.assertEqual(ids, ["new_offset", "old_aligned"])


class MapillaryTests(SourcesTestCase):
    def test_filtering_and_ranking(self):
        with self.env():
            ranked = mapillary.candidates(SITE_LAT, SITE_LON, MLY_IMAGES)
        ids = [c["id"] for c in ranked]
        for dropped in ("far", "close", "misaligned", "pano"):
            self.assertNotIn(dropped, ids)
        self.assertEqual(ids, ["best_s", "east", "older_s", "raw_compass"])

    def test_heading_threshold_env(self):
        with self.env(MAPILLARY_MAX_HEADING_DIFF="3"):
            ids = [c["id"] for c in mapillary.candidates(SITE_LAT, SITE_LON, MLY_IMAGES)]
        self.assertNotIn("east", ids)  # 5 degrees off
        self.assertIn("best_s", ids)

    def test_select_prefers_direction_diversity(self):
        with self.env():
            ranked = mapillary.candidates(SITE_LAT, SITE_LON, MLY_IMAGES)
        self.assertEqual([c["id"] for c in mapillary.select(ranked, 2)], ["best_s", "east"])
        three = [c["id"] for c in mapillary.select(ranked, 3)]
        self.assertEqual(three, ["best_s", "east", "raw_compass"])
        # Only same-side images left: fall back to ranking order.
        same_side = [c for c in ranked if c["id"] in ("best_s", "older_s")]
        self.assertEqual([c["id"] for c in mapillary.select(same_side, 2)], ["best_s", "older_s"])
        self.assertEqual(mapillary.select(ranked, 0), [])

    def test_empty_search_is_not_cached(self):
        """The bbox endpoint can return [] then real data; [] must not stick."""
        thumb = jpeg_bytes(textured_image(128))
        replies = [{"data": []}, {"data": []}, {"data": MLY_IMAGES}]

        def handler(url, params, headers):
            if url == mapillary.SEARCH_URL:
                return FakeResp(ctype="application/json", payload=replies.pop(0))
            return FakeResp(content=thumb)

        self.fake(handler)
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN, MAPILLARY_MAX_VIEWS="2"):
            self.assertEqual(mapillary.fetch(SITE_LAT, SITE_LON), [])
            views = mapillary.fetch(SITE_LAT, SITE_LON)
        self.assertEqual([v.meta["image_id"] for v in views], ["best_s", "east"])

    def test_empty_search_retried_once(self):
        thumb = jpeg_bytes(textured_image(128))
        replies = [{"data": []}, {"data": MLY_IMAGES}]

        def handler(url, params, headers):
            if url == mapillary.SEARCH_URL:
                return FakeResp(ctype="application/json", payload=replies.pop(0))
            return FakeResp(content=thumb)

        self.fake(handler)
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN, MAPILLARY_MAX_VIEWS="2"):
            views = mapillary.fetch(SITE_LAT, SITE_LON)
        self.assertEqual([v.meta["image_id"] for v in views], ["best_s", "east"])

    def test_fetch_labels_meter_and_cache(self):
        thumb = jpeg_bytes(textured_image(128))

        def handler(url, params, headers):
            if url == mapillary.SEARCH_URL:
                return FakeResp(ctype="application/json", payload={"data": MLY_IMAGES})
            return FakeResp(content=thumb)

        session = self.fake(handler)
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN, MAPILLARY_MAX_VIEWS="2"):
            with sources.source_meter() as meter:
                views = mapillary.fetch(SITE_LAT, SITE_LON)
            self.assertEqual([v.meta["image_id"] for v in views], ["best_s", "east"])
            self.assertEqual((meter.requests, meter.billable, meter.cache_hits), (3, 0, 0))

            search = session.calls[0]
            self.assertEqual(search["headers"]["Authorization"], f"OAuth {MAPILLARY_TOKEN}")
            self.assertNotIn("access_token", search["params"])
            self.assertIn("computed_compass_angle", search["params"]["fields"])
            self.assertEqual(search["params"]["limit"], 100)
            minlon, minlat, maxlon, maxlat = (float(v) for v in search["params"]["bbox"].split(","))
            self.assertAlmostEqual(base.haversine_m(minlat, SITE_LON, maxlat, SITE_LON), 200.0, delta=0.5)
            self.assertLess(minlon, SITE_LON)
            self.assertGreater(maxlon, SITE_LON)

            first = views[0]
            self.assertEqual(first.source, "mapillary")
            self.assertEqual(first.captured, "2024-06-01")
            self.assertEqual(first.label,
                             "Street-level photo (Mapillary 2024-06, camera 30 m S of site, facing N toward site, cropped toward site)")
            self.assertEqual(first.meta["distance_m"], 30.0)
            for view in views:
                self.assertIn("Street-level", view.label)
                self.assertNotIn("top-down", view.label.lower())
                self.assertNotIn("oblique", view.label.lower())
                self.assertFalse(is_oblique_label(view.label))
                self.assertFalse(is_top_down_label(view.label))

            with sources.source_meter() as meter:
                again = mapillary.fetch(SITE_LAT, SITE_LON)
            self.assertEqual(len(again), 2)
            self.assertEqual((meter.requests, meter.cache_hits), (0, 3))

    def test_max_views_env(self):
        thumb = jpeg_bytes(textured_image(64))
        self.fake(lambda url, params, headers: FakeResp(
            ctype="application/json", payload={"data": MLY_IMAGES})
            if url == mapillary.SEARCH_URL else FakeResp(content=thumb))
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN, MAPILLARY_MAX_VIEWS="1"):
            views = mapillary.fetch(SITE_LAT, SITE_LON)
        self.assertEqual([v.meta["image_id"] for v in views], ["best_s"])

    def test_street_level_labels_survive_trim(self):
        img = textured_image(32)
        views = [("NAIP top-down", img), ("Nearmap oblique (North)", img),
                 ("Nearmap oblique (East)", img), ("Nearmap oblique (South)", img),
                 (mapillary.label_for({"distance_m": 30, "approach": 270, "heading": 90}, "2024-06-01"), img),
                 (mapillary.label_for({"distance_m": 22, "approach": 225, "heading": 45}, "2023-07-01"), img)]
        labels = [label for label, _img in trim_views_for_model(views, max_obliques=2)]
        self.assertEqual(sum("Street-level" in label for label in labels), 2)


# ------------------------------ meter / registry ------------------------------


class MeterTests(SourcesTestCase):
    def test_as_row(self):
        meter = sources.SourceMeter()
        meter.add_request()
        meter.add_request(billable=True)
        meter.add_cache_hit()
        meter.add_views("mapillary", 2)
        meter.add_views("mapillary", 0)
        self.assertEqual(meter.as_row(), {
            "supplemental_sources": "mapillary:2",
            "supplemental_requests": 2,
            "supplemental_billable": 1,
            "supplemental_cache_hits": 1,
        })
        self.assertEqual(sources.SourceMeter().as_row()["supplemental_sources"], "")

    def test_meter_is_thread_local_and_nests(self):
        seen = {}

        def worker():
            seen["other"] = base.current_meter()

        with sources.source_meter() as outer:
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
            with sources.source_meter() as inner:
                base.record_request()
            self.assertIs(base.current_meter(), outer)
        self.assertIsNone(seen["other"])
        self.assertEqual((outer.requests, inner.requests), (0, 1))
        self.assertIsNone(base.current_meter())


class RegistryTests(SourcesTestCase):
    def _street_view(self):
        return base.SupplementalView(label="Street-level photo (Mapillary)", image=textured_image(32),
                                     source="mapillary", captured=None, meta={})

    def test_fail_open_redacts_keys(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError(
                "Max retries exceeded with url: /images?access_token=" + MAPILLARY_TOKEN)

        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN, SUPPLEMENTAL_IMAGERY="mapillary"), \
                patch.object(mapillary, "fetch", side_effect=boom):
            with self.assertLogs("classifier.sources", level="WARNING") as logs:
                views = sources.fetch_supplemental_views(SITE_LAT, SITE_LON, site_id="S1")
        self.assertEqual(views, [])
        text = "\n".join(logs.output)
        self.assertIn("mapillary", text)
        self.assertNotIn(MAPILLARY_TOKEN, text)
        self.assertIn("access_token=REDACTED", text)

    def test_max_views_and_chip_files(self):
        chip_dir = self.tmp / "chips"
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN, SUPPLEMENTAL_IMAGERY="mapillary",
                      MAPILLARY_MAX_VIEWS="1"), \
                patch.object(mapillary, "fetch", return_value=[self._street_view(), self._street_view()]):
            with sources.source_meter() as meter:
                views = sources.fetch_supplemental_views(
                    SITE_LAT, SITE_LON, site_id="a0X/123", chip_dir=chip_dir, state="NY")
        self.assertEqual(len(views), 1)
        self.assertTrue((chip_dir / "a0X_123_mapillary_1.jpg").is_file())
        self.assertFalse((chip_dir / "a0X_123_mapillary_2.jpg").exists())
        self.assertEqual(meter.as_row()["supplemental_sources"], "mapillary:1")

    def test_sources_override_drops_unknown(self):
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN), \
                patch.object(mapillary, "fetch", return_value=[self._street_view()]):
            views = sources.fetch_supplemental_views(SITE_LAT, SITE_LON, sources=["mapillary", "nope"])
        self.assertEqual([v.source for v in views], ["mapillary"])

    def test_bad_coordinates_and_nothing_enabled(self):
        with self.env():
            self.assertEqual(sources.fetch_supplemental_views(float("nan"), 0.0), [])
            self.assertEqual(sources.fetch_supplemental_views(None, 0.0), [])  # type: ignore[arg-type]
            self.assertEqual(sources.fetch_supplemental_views(SITE_LAT, SITE_LON), [])

    def test_keys_never_logged(self):
        def handler(url, params, headers):
            raise ConnectionError(f"failed GET {url}?key={MAPILLARY_TOKEN}&token={MAPILLARY_TOKEN}")

        self.fake(handler)
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN,
                      SUPPLEMENTAL_IMAGERY="mapillary,bogus"):
            with self.assertLogs("classifier", level="DEBUG") as logs:
                views = sources.fetch_supplemental_views(SITE_LAT, SITE_LON, site_id="S1")
        self.assertEqual(views, [])
        text = "\n".join(logs.output)
        self.assertIn("ConnectionError", text)
        self.assertNotIn(MAPILLARY_TOKEN, text)

    def test_redact(self):
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN, MAPILLARY_MAX_VIEWS="2"):
            out = base.redact(f"https://x/y?location=1,2&key=AIzaQueryKey987&pano=P "
                              f"Authorization: OAuth {MAPILLARY_TOKEN} raw {MAPILLARY_TOKEN}")
        self.assertNotIn("AIzaQueryKey987", out)
        self.assertNotIn("secret-mapillary", out)
        self.assertIn("location=1,2", out)
        self.assertIn("pano=P", out)


if __name__ == "__main__":
    unittest.main()
