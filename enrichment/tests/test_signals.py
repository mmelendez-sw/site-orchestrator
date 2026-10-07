"""Offline tests for enrichment.signals (fake SQL cursors, patched HTTP)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import enrichment.signals as signals
from enrichment.signals import opencellid, osm_antenna, uls

KEY = "SECRETKEY123"


def _meters_north(lat: float, meters: float) -> float:
    return lat + meters / 111_195.0


def _hd(usi, call, status="A", service="CF"):
    fields = ["HD", str(usi), "0000000001", "", call, status, service] + [""] * 52
    return "|".join(fields) + "\r\n"


def _lo(usi, call, lat=(40, 30, 0.0, "N"), lon=(74, 15, 36.0, "W"), num=1, typ="F", struct="B"):
    fields = [""] * 51
    fields[0], fields[1], fields[4] = "LO", str(usi), call
    fields[6], fields[7], fields[8] = typ, "T", str(num)
    fields[18] = "12.5"
    fields[19:23] = [str(v) for v in lat]
    fields[23:27] = [str(v) for v in lon]
    fields[38], fields[39], fields[40] = "", "45.0", struct
    return "|".join(fields) + "\r\n"


class FakeCursor:
    """Records SQL; answers bbox/join selects from an in-memory table."""

    def __init__(self, table=None, *, missing=False, error=None):
        self.table = table or []
        self.missing = missing
        self.error = error
        self.executed: list[tuple[str, tuple]] = []
        self.many: list[tuple[str, list]] = []
        self.description = None
        self._rows: list[tuple] = []
        self.points: list[tuple] = []

    def execute(self, sql, *params):
        self.executed.append((sql, params))
        if self.error is not None and "FccUlsMicrowaveLocation" in sql:
            raise self.error
        if self.missing and "FccUlsMicrowaveLocation" in sql:
            raise RuntimeError(
                "('42S02', \"[42S02] Invalid object name 'dbo.FccUlsMicrowaveLocation'.\")"
            )
        if sql.startswith("SELECT p.query_key"):
            self.description = [("query_key",), ("call_sign",), ("latitude",), ("longitude",)]
            self._rows = [
                (key, r["call_sign"], r["latitude"], r["longitude"])
                for key, a, b, c, d in self.points
                for r in self.table
                if a <= r["latitude"] <= b and c <= r["longitude"] <= d
            ]
        elif sql.startswith("SELECT u.call_sign"):
            a, b, c, d = params
            self.description = [("call_sign",), ("latitude",), ("longitude",)]
            self._rows = [
                (r["call_sign"], r["latitude"], r["longitude"])
                for r in self.table
                if a <= r["latitude"] <= b and c <= r["longitude"] <= d
            ]

    def executemany(self, sql, rows):
        self.many.append((sql, list(rows)))
        self.points.extend(rows)

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class _Base(unittest.TestCase):
    def setUp(self):
        signals.reset_warnings()
        uls.reset_table_state()
        opencellid.reset_state(rpm=0)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = {
            "SITE_ORCHESTRATOR_DATA": self._tmp.name,
            "SIGNALS": "1",
            "SIGNALS_SOURCES": "",
            "OPENCELLID_API_KEY": "",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        lim = mock.patch.object(osm_antenna, "_limiter", signals.RateLimiter(0))
        lim.start()
        self.addCleanup(lim.stop)


# ------------------------------------------------------------------ ULS parse


class DmsTests(unittest.TestCase):
    def test_north_east_positive(self):
        self.assertAlmostEqual(uls.dms_to_decimal(40, 30, 0, "N", is_lat=True), 40.5)
        self.assertAlmostEqual(uls.dms_to_decimal("10", "0", "36.0", "E", is_lat=False), 10.01)

    def test_south_west_negative(self):
        self.assertAlmostEqual(uls.dms_to_decimal(33, 52, 4.8, "S", is_lat=True), -(33 + 52 / 60 + 4.8 / 3600))
        self.assertAlmostEqual(uls.dms_to_decimal("74", "15", "36.0", "w", is_lat=False), -74.26)

    def test_blank_seconds_and_minutes(self):
        self.assertAlmostEqual(uls.dms_to_decimal("45", "", "", "N", is_lat=True), 45.0)

    def test_invalid(self):
        self.assertIsNone(uls.dms_to_decimal("", "1", "2", "N", is_lat=True))
        self.assertIsNone(uls.dms_to_decimal("40", "61", "0", "N", is_lat=True))
        self.assertIsNone(uls.dms_to_decimal("40", "0", "60", "N", is_lat=True))
        self.assertIsNone(uls.dms_to_decimal("40", "0", "0", "W", is_lat=True))
        self.assertIsNone(uls.dms_to_decimal("40", "0", "0", "", is_lat=True))
        self.assertIsNone(uls.dms_to_decimal("91", "0", "0", "N", is_lat=True))
        self.assertIsNone(uls.dms_to_decimal("181", "0", "0", "E", is_lat=False))
        self.assertIsNone(uls.dms_to_decimal("x", "0", "0", "E", is_lat=False))


class UlsParseTests(unittest.TestCase):
    def test_parse_hd_fields(self):
        hd = uls.parse_hd(_hd(123, "WQAB123", "A", "MG").rstrip("\r\n").split("|"))
        self.assertEqual(hd, {
            "unique_system_identifier": 123,
            "call_sign": "WQAB123",
            "license_status": "A",
            "radio_service_code": "MG",
        })
        self.assertIsNone(uls.parse_hd(["LO", "1"]))

    def test_parse_lo_fields(self):
        lo = uls.parse_lo(_lo(5, "WQX1", lat=(33, 0, 0.0, "S"), num=3).rstrip("\r\n").split("|"))
        self.assertEqual(lo["unique_system_identifier"], 5)
        self.assertEqual(lo["location_number"], 3)
        self.assertAlmostEqual(lo["latitude"], -33.0)
        self.assertAlmostEqual(lo["longitude"], -74.26)
        self.assertEqual(lo["ground_elevation_m"], 12.5)
        self.assertEqual(lo["structure_height_m"], 45.0)  # overall height fallback
        self.assertEqual(lo["location_type"], "F")
        self.assertEqual(lo["structure_type"], "B")

    def test_parse_lo_bad_coords(self):
        line = _lo(5, "WQX1", lat=("", "", "", "")).rstrip("\r\n").split("|")
        self.assertIsNone(uls.parse_lo(line))

    def test_build_locations_active_only(self):
        hd = [_hd(1, "WA1"), _hd(2, "WC2", status="C"), _hd(3, "WE3", status="E"), _hd(4, "WA4")]
        lo = [
            _lo(1, "WA1", num=1),
            _lo(1, "WA1", num=2, lat=(41, 0, 0, "N")),
            _lo(2, "WC2"),
            _lo(3, "WE3"),
            _lo(4, "WA4", lat=("", "", "", "N")),  # bad coords
            _lo(4, "WA4", num=2, typ="M"),  # mobile area
            _lo(1, "WA1", num=1),  # duplicate
            _lo(9, "WX9"),  # no header
        ]
        rows, stats = uls.build_locations(hd, lo)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["license_status"] == "A" for r in rows))
        self.assertEqual({r["radio_service_code"] for r in rows}, {"CF"})
        self.assertEqual(stats["hd_records"], 4)
        self.assertEqual(stats["hd_active"], 2)
        self.assertEqual(stats["lo_records"], 8)
        self.assertEqual(stats["lo_inactive"], 3)
        self.assertEqual(stats["lo_bad_coords"], 1)
        self.assertEqual(stats["lo_mobile"], 1)
        self.assertEqual(stats["lo_duplicates"], 1)

    def test_iter_records_joins_broken_lines(self):
        line = _lo(1, "WA1").rstrip("\r\n")
        cut = line.index("|", 30)
        lines = [line[:cut] + "\r\n", line[cut:] + "\r\n", _lo(2, "WB2")]
        recs = list(uls.iter_records(lines, "LO"))
        self.assertEqual(len(recs), 2)
        self.assertEqual(len(recs[0]), 51)


class LoaderScriptTests(unittest.TestCase):
    def _module(self):
        path = Path(__file__).resolve().parents[2] / "scripts" / "load_fcc_uls_microwave.py"
        spec = importlib.util.spec_from_file_location("load_fcc_uls_microwave", path)
        mod = importlib.util.module_from_spec(spec)
        with mock.patch("dotenv.load_dotenv"):  # keep the real .env out of tests
            spec.loader.exec_module(mod)
        return mod

    def test_dry_run_on_synthetic_zip(self):
        import zipfile

        mod = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "l_micro.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("HD.dat", _hd(1, "WA1") + _hd(2, "WC2", status="C"))
                zf.writestr("LO.dat", _lo(1, "WA1") + _lo(2, "WC2"))
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf), mock.patch.object(mod, "load") as load:
                self.assertEqual(mod.main(["--zip", str(zpath), "--dry-run"]), 0)
            load.assert_not_called()
            self.assertIn("locations=1", buf.getvalue())

    def test_ddl_batches_split_on_go(self):
        mod = self._module()
        batches = mod.ddl_batches(mod.DDL_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(batches), 4)
        self.assertTrue(all("GO" not in b.split() for b in batches))
        self.assertIn("CREATE TABLE dbo.FccUlsMicrowaveLocation", batches[0])
        self.assertEqual(len(mod.row_tuple({"call_sign": None})), len(mod.INSERT_COLUMNS))


# ----------------------------------------------------------------- ULS lookup


class UlsLookupTests(_Base):
    LAT, LON = 40.0, -75.0

    def _table(self):
        return [
            {"call_sign": "WNEAR", "latitude": _meters_north(self.LAT, 10), "longitude": self.LON},
            {"call_sign": "WMID", "latitude": _meters_north(self.LAT, 100), "longitude": self.LON},
            {"call_sign": "WNEAR", "latitude": _meters_north(self.LAT, 20), "longitude": self.LON},
            {"call_sign": "WFAR", "latitude": _meters_north(self.LAT, 160), "longitude": self.LON},
            {"call_sign": "WX4", "latitude": _meters_north(self.LAT, 120), "longitude": self.LON},
            {"call_sign": "WX5", "latitude": _meters_north(self.LAT, 130), "longitude": self.LON},
        ]

    def test_summarize_nearest_count_radius(self):
        out = uls.summarize(self.LAT, self.LON, self._table(), radius_m=150)
        self.assertAlmostEqual(out["uls_nearest_m"], 10.0, delta=0.2)
        self.assertEqual(out["uls_count"], 5)
        self.assertEqual(out["uls_call_signs"], "WNEAR;WMID;WX4")

    def test_summarize_none_found(self):
        out = uls.summarize(self.LAT, self.LON, [], radius_m=150)
        self.assertEqual(out, {"uls_nearest_m": "", "uls_count": 0, "uls_call_signs": ""})

    def test_single_lookup_uses_bbox(self):
        cur = FakeCursor(self._table())
        out = uls.lookup(cur, self.LAT, self.LON, radius_m=150)
        self.assertEqual(out["uls_count"], 5)
        sql, params = cur.executed[0]
        self.assertIn("dbo.FccUlsMicrowaveLocation", sql)
        self.assertEqual(len(params), 4)

    def test_bulk_keyed(self):
        cur = FakeCursor(self._table())
        pts = {"a0000000000000001": (self.LAT, self.LON), "b": (10.0, 10.0)}
        out = uls.lookup_bulk(cur, pts, radius_m=150, chunk_size=1)
        self.assertEqual(out["a0000000000000001"]["uls_count"], 5)
        self.assertEqual(out["b"]["uls_count"], 0)
        self.assertEqual(len(cur.many), 2)  # one temp-table load per chunk
        self.assertTrue(any("#signals_uls_points" in s for s, _ in cur.executed))

    def test_missing_table_blank_and_warns_once(self):
        cur = FakeCursor(missing=True)
        with self.assertLogs("enrichment.signals", level="WARNING") as logs:
            self.assertIsNone(uls.lookup(cur, self.LAT, self.LON))
            self.assertIsNone(uls.lookup(cur, self.LAT, self.LON))
            self.assertEqual(uls.lookup_bulk(cur, {"x": (1.0, 1.0)}), {"x": None})
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(len(cur.executed), 1)  # cached after first miss

    def test_bulk_missing_table(self):
        out = uls.lookup_bulk(FakeCursor(missing=True), {"x": (1.0, 1.0), "y": (2.0, 2.0)})
        self.assertEqual(out, {"x": None, "y": None})

    def test_other_sql_error_raises(self):
        with self.assertRaises(RuntimeError):
            uls.lookup(FakeCursor(error=RuntimeError("deadlock")), self.LAT, self.LON)


# ----------------------------------------------------------------- OpenCelliD


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class OpenCellIdTests(_Base):
    LAT, LON = 40.0, -75.0

    def test_disabled_without_key(self):
        self.assertFalse(opencellid.enabled())
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(opencellid.lookup(self.LAT, self.LON))
        urlopen.assert_not_called()

    def test_parse_cells(self):
        payload = {"count": 2, "cells": [{"lat": 40.001, "lon": -75.0, "radio": "LTE"}, {"lat": "x"}]}
        cells = opencellid.parse_cells(payload)
        self.assertEqual(len(cells), 1)
        self.assertEqual(opencellid.parse_cells({"count": 0, "cells": []}), [])
        self.assertEqual(opencellid.parse_cells({"error": "Cell not found", "code": 1}), [])
        with self.assertRaises(opencellid.OpenCellIdError):
            opencellid.parse_cells({"error": "BBOX too big", "code": 3})

    def test_bbox_under_api_limit(self):
        a, b, c, d = opencellid.query_bbox(40.0, -75.0, 5000)
        from enrichment.geo import haversine_meters

        h = haversine_meters(a, b, c, b)
        w = haversine_meters(a, b, a, d)
        self.assertLess(h * w, opencellid.MAX_BBOX_AREA_M2)

    def test_lookup_count_nearest_and_cache_hit(self):
        payload = {"count": 3, "cells": [
            {"lat": _meters_north(self.LAT, 50), "lon": self.LON},
            {"lat": _meters_north(self.LAT, 250), "lon": self.LON},
            {"lat": _meters_north(self.LAT, 299.9), "lon": self.LON + 0.002},  # corner, outside radius
        ]}
        with mock.patch.dict(os.environ, {"OPENCELLID_API_KEY": KEY}), \
                mock.patch("urllib.request.urlopen", return_value=_Resp(payload)) as urlopen:
            out = opencellid.lookup(self.LAT, self.LON, radius_m=300)
            again = opencellid.lookup(self.LAT, self.LON, radius_m=300)
        self.assertEqual(out["opencellid_count"], 2)
        self.assertAlmostEqual(out["opencellid_nearest_m"], 50.0, delta=0.3)
        self.assertEqual(out, again)
        self.assertEqual(urlopen.call_count, 1)
        url = urlopen.call_args[0][0].full_url
        self.assertIn("BBOX=", url)
        self.assertIn("format=json", url)
        cached = list((Path(self._tmp.name) / "cache" / "opencellid").glob("*.json"))
        self.assertEqual(len(cached), 1)
        self.assertNotIn(KEY, cached[0].read_text(encoding="utf-8"))

    def test_key_redacted_in_errors_and_logs(self):
        err = urllib.error.HTTPError(
            f"https://opencellid.org/cell/getInArea?key={KEY}", 401, "Unauthorized", {},
            io.BytesIO(f'{{"error":"API Key not known: {KEY}","code":2}}'.encode()),
        )
        with mock.patch.dict(os.environ, {"OPENCELLID_API_KEY": KEY, "SIGNALS_SOURCES": "opencellid"}), \
                mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertLogs("enrichment.signals", level="DEBUG") as logs:
                row = signals.collect_signals(self.LAT, self.LON)
            self.assertFalse(opencellid.enabled())  # disabled after 401
        text = "\n".join(logs.output)
        self.assertNotIn(KEY, text)
        self.assertIn("***", text)
        self.assertEqual(row["opencellid_count"], "")
        self.assertEqual(row["signal_strength"], "")

    def test_redact_handles_url_encoding(self):
        self.assertEqual(opencellid.redact("k=a%2Bb and a+b", key="a+b"), "k=*** and ***")

    def test_rate_limiter_spacing(self):
        now = [0.0]
        slept = []
        lim = signals.RateLimiter(30, clock=lambda: now[0], sleep=slept.append)
        lim.wait()
        lim.wait()
        self.assertEqual(slept, [2.0])


# ----------------------------------------------------------------------- OSM


class OsmAntennaTests(_Base):
    LAT, LON = 40.0, -75.0

    def test_classification(self):
        c = osm_antenna.classify_element
        self.assertEqual(c({"tags": {"man_made": "mast", "tower:type": "communication"}}), "telecom")
        self.assertEqual(c({"tags": {"man_made": "antenna", "communication:mobile_phone": "yes"}}), "telecom")
        self.assertEqual(c({"tags": {"telecom": "antenna"}}), "telecom")
        self.assertEqual(c({"tags": {"man_made": "antenna"}}), "antenna")
        self.assertIsNone(c({"tags": {"man_made": "mast", "tower:type": "lighting"}}))
        self.assertIsNone(c({"tags": {"man_made": "tower", "tower:type": "observation"}}))
        self.assertIsNone(c({"tags": {"building": "yes"}}))
        self.assertIsNone(c({}))

    def test_summarize_counts_and_telecom_distance(self):
        els = [
            {"type": "node", "id": 1, "lat": _meters_north(self.LAT, 20), "lon": self.LON,
             "tags": {"man_made": "mast", "tower:type": "communication"}},
            {"type": "node", "id": 1, "lat": _meters_north(self.LAT, 20), "lon": self.LON,
             "tags": {"man_made": "mast", "tower:type": "communication"}},  # dup
            {"type": "way", "id": 2, "center": {"lat": _meters_north(self.LAT, 40), "lon": self.LON},
             "tags": {"man_made": "antenna"}},
            {"type": "node", "id": 3, "lat": _meters_north(self.LAT, 90), "lon": self.LON,
             "tags": {"man_made": "antenna"}},  # outside 60 m
            {"type": "node", "id": 4, "lat": self.LAT, "lon": self.LON, "tags": {"man_made": "mast"}},
        ]
        out = osm_antenna.summarize(self.LAT, self.LON, els, radius_m=60)
        self.assertEqual(out["osm_antenna_count"], 2)
        self.assertAlmostEqual(out[signals.OSM_TELECOM_NEAREST_KEY], 20.0, delta=0.2)

    def test_query_mentions_tags(self):
        q = osm_antenna.overpass_query(self.LAT, self.LON, 60)
        for needle in ("man_made", "tower:type", "communication:mobile_phone", "telecom", "around:60"):
            self.assertIn(needle, q)

    def test_fetch_cached(self):
        payload = {"elements": [{"type": "node", "id": 5, "lat": self.LAT, "lon": self.LON,
                                 "tags": {"man_made": "antenna"}}]}
        with mock.patch("urllib.request.urlopen", return_value=_Resp(payload)) as urlopen:
            a = osm_antenna.lookup(self.LAT, self.LON)
            b = osm_antenna.lookup(self.LAT, self.LON)
        self.assertEqual(a, b)
        self.assertEqual(a["osm_antenna_count"], 1)
        self.assertEqual(urlopen.call_count, 1)

    def test_failure_raises_and_is_not_cached(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(urllib.error.URLError):
                osm_antenna.lookup(self.LAT, self.LON)
        self.assertFalse((Path(self._tmp.name) / "cache" / "osm_antenna").exists())


# ------------------------------------------------------------ strength + env


class StrengthAndEnvTests(_Base):
    def test_strength_rules(self):
        s = signals.signal_strength
        self.assertEqual(s({}, set()), "")
        self.assertEqual(s({"uls_count": 0, "osm_antenna_count": 0}, {"uls", "osm"}), "none")
        self.assertEqual(s({"uls_nearest_m": 25.0, "uls_count": 1}, {"uls"}), "strong")
        self.assertEqual(s({"uls_nearest_m": 31.0, "uls_count": 1}, {"uls"}), "weak")
        self.assertEqual(s({"opencellid_count": 3, "opencellid_nearest_m": 5.0}, {"opencellid"}), "weak")
        self.assertEqual(s({"osm_antenna_count": 1, signals.OSM_TELECOM_NEAREST_KEY: 12.0}, {"osm"}), "strong")
        self.assertEqual(s({"osm_antenna_count": 1, signals.OSM_TELECOM_NEAREST_KEY: ""}, {"osm"}), "weak")
        with mock.patch.dict(os.environ, {"ULS_STRONG_M": "40"}):
            self.assertEqual(s({"uls_nearest_m": 31.0, "uls_count": 1}, {"uls"}), "strong")

    def test_signals_enabled_parsing(self):
        for raw, want in (("", False), ("0", False), ("1", True), ("true", True), ("no", False)):
            with mock.patch.dict(os.environ, {"SIGNALS": raw}):
                self.assertEqual(signals.signals_enabled(), want, raw)

    def test_sources_parsing(self):
        cases = {
            "": ("uls", "opencellid", "osm"),
            "osm": ("osm",),
            " OSM , uls ,bogus": ("uls", "osm"),
            "opencellid,osm_antenna": ("opencellid", "osm"),
        }
        for raw, want in cases.items():
            with mock.patch.dict(os.environ, {"SIGNALS_SOURCES": raw}):
                self.assertEqual(signals.enabled_sources(), want, raw)

    def test_disabled_returns_blank_row(self):
        with mock.patch.dict(os.environ, {"SIGNALS": "0"}):
            row = signals.collect_signals(40.0, -75.0, cursor=FakeCursor())
        self.assertEqual(row, {k: "" for k in signals.SIGNAL_COLUMNS})


class CollectTests(_Base):
    LAT, LON = 40.0, -75.0

    def _osm_ok(self, *_a, **_k):
        return {"osm_antenna_count": 0, signals.OSM_TELECOM_NEAREST_KEY: ""}

    def test_all_columns_and_strong_from_uls(self):
        table = [{"call_sign": "WROOF", "latitude": _meters_north(self.LAT, 5), "longitude": self.LON}]
        with mock.patch.object(osm_antenna, "lookup", self._osm_ok):
            row = signals.collect_signals(self.LAT, self.LON, cursor=FakeCursor(table))
        self.assertEqual(tuple(row), signals.SIGNAL_COLUMNS)
        self.assertEqual(row["uls_count"], 1)
        self.assertEqual(row["uls_call_signs"], "WROOF")
        self.assertEqual(row["opencellid_count"], "")  # no key
        self.assertEqual(row["osm_antenna_count"], 0)
        self.assertEqual(row["signal_strength"], "strong")

    def test_none_when_sources_ran_empty(self):
        with mock.patch.object(osm_antenna, "lookup", self._osm_ok):
            row = signals.collect_signals(self.LAT, self.LON, cursor=FakeCursor([]))
        self.assertEqual(row["uls_count"], 0)
        self.assertEqual(row["signal_strength"], "none")

    def test_never_raises_when_sources_throw(self):
        boom = mock.Mock(side_effect=RuntimeError("kaboom"))
        with mock.patch.dict(os.environ, {"OPENCELLID_API_KEY": KEY}), \
                mock.patch.object(uls, "lookup", boom), \
                mock.patch.object(opencellid, "lookup", boom), \
                mock.patch.object(osm_antenna, "lookup", boom), \
                self.assertLogs("enrichment.signals", level="WARNING") as logs:
            row = signals.collect_signals(self.LAT, self.LON, cursor=FakeCursor())
            row2 = signals.collect_signals(self.LAT, self.LON, cursor=FakeCursor())
        self.assertEqual(row, {k: "" for k in signals.SIGNAL_COLUMNS})
        self.assertEqual(row, row2)
        self.assertEqual(len(logs.records), 3)  # once per source per process

    def test_no_cursor_skips_uls(self):
        with mock.patch.dict(os.environ, {"SIGNALS_SOURCES": "uls"}):
            row = signals.collect_signals(self.LAT, self.LON)
        self.assertEqual(row["uls_count"], "")
        self.assertEqual(row["signal_strength"], "")

    def test_invalid_point(self):
        row = signals.collect_signals(float("nan"), self.LON, cursor=FakeCursor())
        self.assertEqual(row["signal_strength"], "")

    def test_missing_table_blank_but_others_run(self):
        with mock.patch.object(osm_antenna, "lookup", self._osm_ok):
            row = signals.collect_signals(self.LAT, self.LON, cursor=FakeCursor(missing=True))
        self.assertEqual(row["uls_count"], "")
        self.assertEqual(row["osm_antenna_count"], 0)
        self.assertEqual(row["signal_strength"], "none")

    def test_bulk_keyed(self):
        table = [{"call_sign": "WROOF", "latitude": _meters_north(self.LAT, 5), "longitude": self.LON}]
        pts = {"a1": (self.LAT, self.LON), "b2": (10.0, 10.0), "bad": (None, None)}
        cur = FakeCursor(table)
        with mock.patch.dict(os.environ, {"SIGNALS_SOURCES": "uls,osm"}), \
                mock.patch.object(osm_antenna, "lookup", self._osm_ok):
            out = signals.collect_signals_bulk(pts, cursor=cur)
        self.assertEqual(set(out), {"a1", "b2", "bad"})
        self.assertEqual(out["a1"]["signal_strength"], "strong")
        self.assertEqual(out["b2"]["signal_strength"], "none")
        self.assertEqual(out["bad"], {k: "" for k in signals.SIGNAL_COLUMNS})
        self.assertEqual(len(cur.many), 1)  # single temp-table load

    def test_bulk_uls_error_blank_uls_only(self):
        with mock.patch.dict(os.environ, {"SIGNALS_SOURCES": "uls,osm"}), \
                mock.patch.object(osm_antenna, "lookup", self._osm_ok), \
                mock.patch.object(uls, "lookup_bulk", side_effect=RuntimeError("link down")):
            with self.assertLogs("enrichment.signals", level="WARNING"):
                out = signals.collect_signals_bulk({"a": (self.LAT, self.LON)}, cursor=FakeCursor())
        self.assertEqual(out["a"]["uls_count"], "")
        self.assertEqual(out["a"]["osm_antenna_count"], 0)
        self.assertEqual(out["a"]["signal_strength"], "none")

    def test_bulk_disabled(self):
        with mock.patch.dict(os.environ, {"SIGNALS": "0"}):
            out = signals.collect_signals_bulk({"a": (self.LAT, self.LON)}, cursor=FakeCursor())
        self.assertEqual(out, {"a": {k: "" for k in signals.SIGNAL_COLUMNS}})


if __name__ == "__main__":
    unittest.main()
