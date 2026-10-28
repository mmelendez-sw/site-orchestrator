from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from enrichment import osm_prefilter as o


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok(elements):
    return _Resp(json.dumps({"elements": elements}).encode("utf-8"))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"SITE_ORCHESTRATOR_DATA": self.tmp.name, "IMAGERY_CACHE": "1"})
        env.start()
        self.addCleanup(env.stop)
        urls = patch.object(o, "_overpass_urls", return_value=["https://a", "https://b"])
        urls.start()
        self.addCleanup(urls.stop)

    def test_dead_first_server_falls_back_and_caches(self):
        building = [{"type": "way", "tags": {"building": "yes"}}]
        calls = []

        def urlopen(req, timeout):
            calls.append(req.full_url)
            if req.full_url == "https://a":
                raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)
            return _ok(building)

        with patch("urllib.request.urlopen", side_effect=urlopen):
            first = o.lookup_osm_features(40.0, -74.0)
        self.assertTrue(first["ok"])
        self.assertTrue(first["has_building"])
        self.assertEqual(calls, ["https://a", "https://b"])
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            again = o.lookup_osm_features(40.0, -74.0)
        self.assertTrue(again["has_building"])

    def test_all_servers_down_fails_open(self):
        def urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)

        with patch("urllib.request.urlopen", side_effect=urlopen):
            out = o.lookup_osm_features(40.0, -74.0)
        self.assertFalse(out["ok"])
        self.assertFalse(o.osm_suggests_empty_chip(out))


if __name__ == "__main__":
    unittest.main()
