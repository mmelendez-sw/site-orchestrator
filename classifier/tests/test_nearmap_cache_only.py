"""NEARMAP_CACHE_ONLY: Nearmap views rebuilt from cached tiles, never billed."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from classifier import imagery

LAT, LON = 40.0, -74.0


def _tile_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (90, 90, 90)).save(buf, "JPEG")
    return buf.getvalue()


class CacheOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"SITE_ORCHESTRATOR_DATA": self.tmp.name,
                                      "IMAGERY_CACHE": "1", "NEARMAP_TILE_CACHE": "1"})
        env.start()
        self.addCleanup(env.stop)
        net = patch.object(imagery, "_nearmap_get", side_effect=AssertionError("network call"))
        net.start()
        self.addCleanup(net.stop)

    def _fill(self, date: str, view: str, *, skip_one: bool = False):
        zoom = imagery.NEARMAP_VERT_ZOOM if view == "Vert" else imagery.NEARMAP_OBLIQUE_ZOOM
        x0, x1, y0, y1 = imagery._tile_range(LAT, LON, imagery.NEARMAP_CHIP_M / 2.0, zoom)
        d = Path(self.tmp.name) / "cache" / "nearmap" / date / view / str(zoom)
        d.mkdir(parents=True, exist_ok=True)
        data = _tile_bytes()
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                if skip_one and (x, y) == (x0, y0):
                    continue
                (d / f"{x}_{y}.jpg").write_bytes(data)

    def test_rebuilds_fully_cached_views_without_network(self):
        self._fill("2025-03-01", "Vert")
        self._fill("2025-03-01", "North")
        self._fill("2024-01-01", "East")
        views, date = imagery.cached_nearmap_views(LAT, LON, views=["Vert", "North", "East"])
        self.assertEqual(sorted(views), ["East", "North", "Vert"])
        self.assertEqual(date, "2025-03-01")

    def test_partial_tiles_are_not_used(self):
        self._fill("2025-03-01", "North", skip_one=True)
        views, date = imagery.cached_nearmap_views(LAT, LON, views=["North"])
        self.assertEqual(views, {})
        self.assertIsNone(date)

    def test_cache_only_tile_miss_returns_none(self):
        self.assertEqual(imagery._fetch_tile("North", 20, 1, 1, "2025-03-01", cache_only=True),
                         (None, False))


if __name__ == "__main__":
    unittest.main()
