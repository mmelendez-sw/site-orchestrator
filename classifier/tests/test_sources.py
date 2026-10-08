"""Supplemental imagery sources (fully offline: the HTTP session is faked)."""

from __future__ import annotations

import io
import json
import math
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
from classifier.sources import base, mapillary, state_ortho
from classifier.views import is_oblique_label, is_top_down_label, trim_views_for_model

SITE_LAT, SITE_LON = 40.0, -74.0
MAPILLARY_TOKEN = "MLY|secret-mapillary-token-123"

SOURCE_ENV = (
    "SUPPLEMENTAL_IMAGERY", "STATE_ORTHO_SOURCES", "STATE_ORTHO_MAX_VIEWS",
    "MAPILLARY_ACCESS_TOKEN", "MAPILLARY_RADIUS_M", "MAPILLARY_MAX_HEADING_DIFF",
    "MAPILLARY_MAX_VIEWS", "MAPILLARY_SEARCH_CACHE_DAYS",
    "SUPPLEMENTAL_TIMEOUT_S", "IMAGERY_CACHE", "SITE_ORCHESTRATOR_DATA",
    "MAPILLARY_CROP", "MAPILLARY_HFOV", "MAPILLARY_CROP_WIDTH", "MAPILLARY_CROP_TOP",
    "STREET_CONFIRM_MAX_AGE_YEARS",
    "ORTHO_TEST_TOKEN",
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

    def write_config(self, entries) -> Path:
        path = self.tmp / "ortho.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path


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

    def test_mercator_bbox_ground_footprint(self):
        for lat in (0.0, 40.0, 60.0):
            minx, miny, maxx, maxy = base.mercator_bbox(lat, -74.0, 120.0)
            self.assertAlmostEqual(maxx - minx, maxy - miny, places=6)
            # Projected metres x cos(lat) = ground metres.
            self.assertAlmostEqual((maxx - minx) * math.cos(math.radians(lat)), 120.0, places=3)
            cx, cy = base.lonlat_to_mercator(-74.0, lat)
            self.assertAlmostEqual((minx + maxx) / 2, cx, places=6)
            self.assertAlmostEqual((miny + maxy) / 2, cy, places=6)
        # Ground check: the bbox corners are ~120 m apart along a parallel.
        minx, _miny, maxx, _maxy = base.mercator_bbox(60.0, 10.0, 120.0)
        lon_w = math.degrees(minx / base.MERCATOR_R)
        lon_e = math.degrees(maxx / base.MERCATOR_R)
        self.assertAlmostEqual(base.haversine_m(60.0, lon_w, 60.0, lon_e), 120.0, delta=1.0)

    def test_iso_date_from_epoch_ms(self):
        self.assertEqual(base.iso_date_from_epoch_ms(1641206630491), "2022-01-03")
        self.assertIsNone(base.iso_date_from_epoch_ms(None))

    def test_blank_image_detection(self):
        self.assertTrue(base.is_blank_image(Image.new("RGB", (200, 200), (255, 255, 255))))
        self.assertTrue(base.is_blank_image(Image.new("RGB", (200, 200), (0, 0, 0))))
        mostly = Image.new("RGB", (200, 200), (255, 255, 255))
        mostly.paste(textured_image(20), (0, 0))  # 1% real pixels at an edge
        self.assertTrue(base.is_blank_image(mostly))
        self.assertFalse(base.is_blank_image(textured_image(200)))


# ----------------------------- enabled_sources ------------------------------


class EnabledSourcesTests(SourcesTestCase):
    def test_unset_and_none(self):
        with self.env():
            self.assertEqual(sources.enabled_sources(), [])
        with self.env(SUPPLEMENTAL_IMAGERY="none", MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
            self.assertEqual(sources.enabled_sources(), [])

    def test_unknown_ignored_and_missing_credentials_dropped(self):
        with self.env(SUPPLEMENTAL_IMAGERY="mapillary,bogus,streetview,state_ortho",
                      MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
            with self.assertLogs("classifier.sources", level="WARNING") as logs:
                self.assertEqual(sources.enabled_sources(), ["mapillary"])
        text = "\n".join(logs.output)
        self.assertIn("bogus", text)
        self.assertIn("streetview", text)
        self.assertIn("STATE_ORTHO_SOURCES", text)
        self.assertNotIn(MAPILLARY_TOKEN, text)

    def test_logged_once(self):
        with self.env(SUPPLEMENTAL_IMAGERY="bogus"):
            with self.assertLogs("classifier.sources", level="WARNING") as logs:
                sources.enabled_sources()
                sources.enabled_sources()
                base.logger.warning("sentinel")
        self.assertEqual(sum("bogus" in line for line in logs.output), 1)

    def test_order_is_priority_and_dedupe(self):
        cfg = self.write_config([{"name": "X", "states": ["NY"], "type": "wms",
                                  "url": "https://x.invalid/wms", "layer": "o"}])
        with self.env(SUPPLEMENTAL_IMAGERY=" Mapillary , state_ortho, MAPILLARY",
                      MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN, STATE_ORTHO_SOURCES=cfg):
            self.assertEqual(sources.enabled_sources(), ["mapillary", "state_ortho"])
        with self.env(SUPPLEMENTAL_IMAGERY="state_ortho,mapillary", STATE_ORTHO_SOURCES=cfg,
                      MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
            self.assertEqual(sources.enabled_sources(), ["state_ortho", "mapillary"])


# -------------------------------- state ortho --------------------------------


ENTRIES = [
    {"_comment": "ignored"},
    {"name": "County", "states": ["ZZ"], "bbox": [-75.0, 39.5, -73.5, 40.5],
     "type": "wms", "url": "https://county.invalid/ows", "layer": "ortho", "year": 2023,
     "resolution_cm": 7.5, "chip_m": 100, "px": 512},
    {"name": "NYS 2024", "states": ["NY"], "type": "arcgis_image",
     "url": "https://state.invalid/arcgis/rest/services/Ortho/ImageServer/", "year": 2024,
     "resolution_cm": 15},
    {"name": "Map", "states": ["PA"], "type": "arcgis_map",
     "url": "https://pa.invalid/arcgis/rest/services/Ortho/MapServer", "layer": "3"},
    {"name": "Tokened", "states": ["TX"], "type": "arcgis_image",
     "url": "https://tx.invalid/ImageServer", "token_env": "ORTHO_TEST_TOKEN"},
]


class StateOrthoTests(SourcesTestCase):
    def test_config_matching(self):
        cfg = self.write_config(ENTRIES)
        with self.env(STATE_ORTHO_SOURCES=cfg):
            entries = state_ortho.load_config()
            self.assertEqual([e["name"] for e in entries], ["County", "NYS 2024", "Map", "Tokened"])
            match = state_ortho.match_entry
            self.assertEqual(match(SITE_LAT, SITE_LON, None)["name"], "County")       # bbox only
            self.assertEqual(match(SITE_LAT, SITE_LON, "zz")["name"], "County")       # both
            self.assertEqual(match(SITE_LAT, SITE_LON, "ny")["name"], "NYS 2024")     # state
            self.assertEqual(match(42.0, -76.0, "NY")["name"], "NYS 2024")
            self.assertIsNone(match(42.0, -76.0, None))       # no state, outside bbox
            self.assertIsNone(match(42.0, -76.0, "ZZ"))       # state ok, outside bbox
            self.assertIsNone(match(42.0, -76.0, "CA"))
            self.assertIsNone(match(30.0, -97.0, "TX"))       # token env missing
        with self.env(STATE_ORTHO_SOURCES=cfg, ORTHO_TEST_TOKEN="tok-abc-123"):
            self.assertEqual(state_ortho.match_entry(30.0, -97.0, "TX")["name"], "Tokened")

    def test_request_params_per_type(self):
        cfg = self.write_config(ENTRIES)
        with self.env(STATE_ORTHO_SOURCES=cfg, ORTHO_TEST_TOKEN="tok-abc-123"):
            by_name = {e["name"]: e for e in state_ortho.load_config()}

            url, params = state_ortho.build_request(by_name["NYS 2024"], SITE_LAT, SITE_LON)
            self.assertEqual(url, "https://state.invalid/arcgis/rest/services/Ortho/ImageServer/exportImage")
            self.assertEqual(params["bboxSR"], "3857")
            self.assertEqual(params["imageSR"], "3857")
            self.assertEqual(params["size"], "1024,1024")
            self.assertEqual(params["format"], "jpg")
            self.assertEqual(params["f"], "image")
            minx, miny, maxx, maxy = (float(v) for v in params["bbox"].split(","))
            self.assertAlmostEqual((maxx - minx) * math.cos(math.radians(SITE_LAT)), 120.0, delta=0.01)

            url, params = state_ortho.build_request(by_name["Map"], SITE_LAT, SITE_LON)
            self.assertTrue(url.endswith("/MapServer/export"))
            self.assertEqual(params["transparent"], "false")
            self.assertEqual(params["layers"], "show:3")
            self.assertEqual(params["f"], "image")

            url, params = state_ortho.build_request(by_name["County"], SITE_LAT, SITE_LON)
            self.assertEqual(url, "https://county.invalid/ows")
            self.assertEqual(params["REQUEST"], "GetMap")
            self.assertEqual(params["VERSION"], "1.3.0")
            self.assertEqual(params["CRS"], "EPSG:3857")
            self.assertEqual(params["LAYERS"], "ortho")
            self.assertEqual(params["FORMAT"], "image/jpeg")
            self.assertEqual(params["STYLES"], "")
            self.assertEqual((params["WIDTH"], params["HEIGHT"]), ("512", "512"))
            minx, _miny, maxx, _maxy = (float(v) for v in params["BBOX"].split(","))
            self.assertAlmostEqual((maxx - minx) * math.cos(math.radians(SITE_LAT)), 100.0, delta=0.01)

            _url, params = state_ortho.build_request(by_name["Tokened"], 30.0, -97.0)
            self.assertEqual(params["token"], "tok-abc-123")

    def test_fetch_label_and_cache_hit(self):
        cfg = self.write_config(ENTRIES)
        data = jpeg_bytes(textured_image(256))
        session = self.fake(lambda url, params, headers: FakeResp(content=data))
        with self.env(STATE_ORTHO_SOURCES=cfg):
            with sources.source_meter() as meter:
                views = state_ortho.fetch(42.0, -76.0, state="NY")
            self.assertEqual(len(views), 1)
            view = views[0]
            self.assertEqual(view.source, "state_ortho")
            self.assertIn("State orthoimagery top-down", view.label)
            self.assertIn("NY 2024", view.label)
            self.assertIn("~15 cm", view.label)
            self.assertTrue(is_top_down_label(view.label))
            self.assertFalse(is_oblique_label(view.label))
            self.assertEqual(view.captured, "2024")
            self.assertEqual(view.meta["service"], "NYS 2024")
            self.assertEqual((meter.requests, meter.billable, meter.cache_hits), (1, 0, 0))

            with sources.source_meter() as meter:
                again = state_ortho.fetch(42.0, -76.0, state="NY")
            self.assertEqual(len(again), 1)
            self.assertEqual((meter.requests, meter.cache_hits), (0, 1))
            self.assertEqual(len(session.calls), 1)
            self.assertTrue(any((self.tmp / "data" / "cache" / "state_ortho").rglob("*.img")))

    def test_cache_disabled(self):
        cfg = self.write_config(ENTRIES)
        data = jpeg_bytes(textured_image(128))
        session = self.fake(lambda url, params, headers: FakeResp(content=data))
        with self.env(STATE_ORTHO_SOURCES=cfg, IMAGERY_CACHE="0"):
            state_ortho.fetch(42.0, -76.0, state="NY")
            state_ortho.fetch(42.0, -76.0, state="NY")
        self.assertEqual(len(session.calls), 2)

    def test_blank_and_non_image_rejected(self):
        cfg = self.write_config(ENTRIES)
        blank = jpeg_bytes(Image.new("RGB", (256, 256), (255, 255, 255)))
        self.fake(lambda url, params, headers: FakeResp(content=blank))
        with self.env(STATE_ORTHO_SOURCES=cfg):
            self.assertEqual(state_ortho.fetch(42.0, -76.0, state="NY"), [])
            self.assertFalse(any((self.tmp / "data" / "cache").rglob("*.img")))

        err = json.dumps({"error": {"code": 400}}).encode()
        self.fake(lambda url, params, headers: FakeResp(content=err, ctype="application/json"))
        with self.env(STATE_ORTHO_SOURCES=cfg):
            self.assertEqual(state_ortho.fetch(42.0, -76.0, state="NY"), [])

        self.fake(lambda url, params, headers: FakeResp(status=500, content=b""))
        with self.env(STATE_ORTHO_SOURCES=cfg):
            self.assertEqual(state_ortho.fetch(42.0, -76.0, state="NY"), [])

    def test_no_matching_entry_makes_no_request(self):
        cfg = self.write_config(ENTRIES)
        session = self.fake(lambda url, params, headers: FakeResp())
        with self.env(STATE_ORTHO_SOURCES=cfg):
            self.assertEqual(state_ortho.fetch(35.0, -120.0, state="CA"), [])
        self.assertEqual(session.calls, [])

    def test_example_config_parses_and_uses_fake_urls(self):
        example = Path(__file__).resolve().parents[2] / "docs" / "state_ortho_sources.example.json"
        with self.env(STATE_ORTHO_SOURCES=example):
            entries = state_ortho.load_config()
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(".invalid/" in e["url"] for e in entries))


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
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
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
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
            views = mapillary.fetch(SITE_LAT, SITE_LON)
        self.assertEqual([v.meta["image_id"] for v in views], ["best_s", "east"])

    def test_fetch_labels_meter_and_cache(self):
        thumb = jpeg_bytes(textured_image(128))

        def handler(url, params, headers):
            if url == mapillary.SEARCH_URL:
                return FakeResp(ctype="application/json", payload={"data": MLY_IMAGES})
            return FakeResp(content=thumb)

        session = self.fake(handler)
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
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
        meter.add_views("state_ortho", 1)
        meter.add_views("mapillary", 2)
        meter.add_views("state_ortho", 0)
        self.assertEqual(meter.as_row(), {
            "supplemental_sources": "mapillary:2,state_ortho:1",
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
    def _ortho_view(self):
        return base.SupplementalView(label="State orthoimagery top-down (NY 2024)",
                                     image=textured_image(64), source="state_ortho",
                                     captured="2024", meta={})

    def test_fail_open_and_chip_files(self):
        cfg = self.write_config(ENTRIES)

        def boom(*_args, **_kwargs):
            raise RuntimeError(
                "Max retries exceeded with url: /images?access_token=" + MAPILLARY_TOKEN)

        chip_dir = self.tmp / "chips"
        with self.env(STATE_ORTHO_SOURCES=cfg, MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN,
                      SUPPLEMENTAL_IMAGERY="mapillary,state_ortho"), \
                patch.object(mapillary, "fetch", side_effect=boom), \
                patch.object(state_ortho, "fetch", return_value=[self._ortho_view(), self._ortho_view()]):
            with self.assertLogs("classifier.sources", level="WARNING") as logs:
                with sources.source_meter() as meter:
                    views = sources.fetch_supplemental_views(
                        SITE_LAT, SITE_LON, site_id="a0X/123", chip_dir=chip_dir, state="NY")
        self.assertEqual(len(views), 1)  # STATE_ORTHO_MAX_VIEWS default 1
        self.assertEqual(views[0].source, "state_ortho")
        self.assertTrue((chip_dir / "a0X_123_state_ortho_1.jpg").is_file())
        self.assertFalse((chip_dir / "a0X_123_state_ortho_2.jpg").exists())
        self.assertEqual(meter.as_row()["supplemental_sources"], "state_ortho:1")
        text = "\n".join(logs.output)
        self.assertIn("mapillary", text)
        self.assertNotIn(MAPILLARY_TOKEN, text)
        self.assertIn("access_token=REDACTED", text)

    def test_order_follows_priority(self):
        cfg = self.write_config(ENTRIES)
        street = base.SupplementalView(label="Street-level photo (Mapillary)", image=textured_image(32),
                                       source="mapillary", captured=None, meta={})
        with self.env(STATE_ORTHO_SOURCES=cfg, MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN), \
                patch.object(mapillary, "fetch", return_value=[street]), \
                patch.object(state_ortho, "fetch", return_value=[self._ortho_view()]):
            views = sources.fetch_supplemental_views(SITE_LAT, SITE_LON,
                                                     sources=["mapillary", "state_ortho", "nope"])
            self.assertEqual([v.source for v in views], ["mapillary", "state_ortho"])
            views = sources.fetch_supplemental_views(SITE_LAT, SITE_LON,
                                                     sources=["state_ortho", "mapillary"])
            self.assertEqual([v.source for v in views], ["state_ortho", "mapillary"])

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
        with self.env(MAPILLARY_ACCESS_TOKEN=MAPILLARY_TOKEN):
            out = base.redact(f"https://x/y?location=1,2&key=AIzaQueryKey987&pano=P "
                              f"Authorization: OAuth {MAPILLARY_TOKEN} raw {MAPILLARY_TOKEN}")
        self.assertNotIn("AIzaQueryKey987", out)
        self.assertNotIn("secret-mapillary", out)
        self.assertIn("location=1,2", out)
        self.assertIn("pano=P", out)


if __name__ == "__main__":
    unittest.main()
