"""Parallel classify, bulk proximity, batched Salesforce writes, and metrics
persistence (no network / paid APIs)."""

from __future__ import annotations

import csv
import json
import os
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from enrichment.constants import DETAIL_CSV, PROXIMITY_MAX_M

FCC_FIELDS = (
    "ID",
    "ASR_Number",
    "Latitude_Decimal",
    "Longitude_Decimal",
    "Latitude_Calculated",
    "Longitude_Calculated",
    "Registration_Type",
    "Record_Type",
    "Entity_Name",
)
TS_FIELDS = (
    "operator_site_identifier",
    "asset_name",
    "asset_type",
    "asset_category",
    "latitude",
    "longitude",
    "fcc_asr_number",
    "street1",
    "city",
    "state",
    "postal_code",
)


def _in_box(lat, lng, box) -> bool:
    min_lat, max_lat, min_lng, max_lng = box
    return (
        lat is not None
        and lng is not None
        and min_lat <= lat <= max_lat
        and min_lng <= lng <= max_lng
    )


def _fcc_in_box(row, box) -> bool:
    return _in_box(row.get("Latitude_Decimal"), row.get("Longitude_Decimal"), box) or _in_box(
        row.get("Latitude_Calculated"), row.get("Longitude_Calculated"), box
    )


def _ts_in_box(row, box) -> bool:
    return _in_box(row.get("latitude"), row.get("longitude"), box)


class SqlEmulatorCursor:
    """Enough of pyodbc + SQL Server to run per-site and bulk proximity queries."""

    def __init__(self, fcc_rows=(), ts_rows=()):
        self.fcc_rows = list(fcc_rows)
        self.ts_rows = list(ts_rows)
        self.points: list[tuple] = []
        self.description = []
        self._pending: list[tuple] = []
        self.executed: list[str] = []

    def _result(self, fields, rows):
        self.description = [(name,) for name in fields]
        self._pending = [tuple(row.get(name) for name in fields) for row in rows]

    def execute(self, sql, *params):
        text = " ".join(sql.split()).lower()
        self.executed.append(text)
        if "create table" in text or text.startswith("drop table"):
            self.points = [] if "create table" in text else self.points
            self._pending = []
            return
        bulk = "p.query_key" in text
        is_fcc = "fcctowerdata" in text
        if bulk:
            fields = ("query_key",) + (FCC_FIELDS if is_fcc else TS_FIELDS)
            rows = []
            for key, *box in self.points:
                for row in self.fcc_rows if is_fcc else self.ts_rows:
                    if (_fcc_in_box if is_fcc else _ts_in_box)(row, box):
                        rows.append({"query_key": key, **row})
            self._result(fields, rows)
            return
        box = tuple(params[:4])
        if is_fcc:
            self._result(FCC_FIELDS, [r for r in self.fcc_rows if _fcc_in_box(r, box)])
        else:
            self._result(TS_FIELDS, [r for r in self.ts_rows if _ts_in_box(r, box)])

    def executemany(self, sql, rows):
        self.points.extend(tuple(r) for r in rows)

    def fetchall(self):
        return list(self._pending)


def _towers():
    rng = random.Random(7)
    fcc, ts = [], []
    for i in range(40):
        lat = 43.0 + rng.uniform(-0.02, 0.02)
        lng = -89.0 + rng.uniform(-0.02, 0.02)
        fcc.append(
            {"ID": i, "ASR_Number": f"A{i}", "Latitude_Decimal": lat,
             "Longitude_Decimal": lng, "Registration_Type": "Monopole"}
        )
    for i in range(20):
        ts.append(
            {"operator_site_identifier": f"T{i}", "asset_type": "tower",
             "latitude": 43.0 + rng.uniform(-0.02, 0.02),
             "longitude": -89.0 + rng.uniform(-0.02, 0.02)}
        )
    return fcc, ts


class BulkProximityTests(unittest.TestCase):
    def test_bulk_matches_per_site_selection(self):
        from enrichment.mssql import (
            ProximityQuery,
            find_proximity_hit,
            find_proximity_hits_bulk,
        )

        fcc, ts = _towers()
        rng = random.Random(11)
        queries = []
        for i in range(60):
            lat = 43.0 + rng.uniform(-0.02, 0.02)
            lng = -89.0 + rng.uniform(-0.02, 0.02)
            addr = (lat + 0.0008, lng - 0.0006) if i % 3 == 0 else (None, None)
            queries.append(ProximityQuery(f"s{i}", lat, lng, *addr))
        bulk = find_proximity_hits_bulk(
            SqlEmulatorCursor(fcc, ts), queries, max_m=PROXIMITY_MAX_M, chunk_size=7
        )
        self.assertEqual(set(bulk), {q.key for q in queries})
        for q in queries:
            single = find_proximity_hit(
                SqlEmulatorCursor(fcc, ts),
                q.lat,
                q.lng,
                max_m=PROXIMITY_MAX_M,
                address_lat=q.address_lat,
                address_lng=q.address_lng,
            )
            got = bulk[q.key]
            self.assertEqual(single is None, got is None, q.key)
            if single is not None:
                self.assertEqual(
                    (single.source, single.record_id, single.selection_reason,
                     single.candidate_count),
                    (got.source, got.record_id, got.selection_reason, got.candidate_count),
                    q.key,
                )

    def test_bulk_uses_one_join_per_table_per_chunk(self):
        from enrichment.mssql import ProximityQuery, find_proximity_hits_bulk

        cursor = SqlEmulatorCursor(*_towers())
        queries = [ProximityQuery(f"s{i}", 43.0, -89.0 + i * 1e-4) for i in range(10)]
        find_proximity_hits_bulk(cursor, queries, max_m=200, chunk_size=10)
        joins = [sql for sql in cursor.executed if "p.query_key" in sql]
        self.assertEqual(len(joins), 2)


class ClusterGroupTests(unittest.TestCase):
    def test_nearby_pins_share_a_group_in_queue_order(self):
        from enrichment.cost_policy import cluster_groups

        points = [
            (43.0, -89.0),
            (44.0, -90.0),
            (43.0001, -89.0),      # ~11 m from #0
            (43.0002, -89.0),      # chains to #2
            (44.0, -90.1),
        ]
        groups = cluster_groups(points, max_m=25)
        self.assertEqual(groups, [[0, 2, 3], [1], [4]])


class _BatchSObject:
    def __init__(self):
        self.calls = []

    def update(self, record_id, payload):
        self.calls.append((record_id, dict(payload)))
        return 204


class _BatchSF:
    def __init__(self, fail_ids=()):
        self.Site__c = _BatchSObject()
        self.fail_ids = set(fail_ids)
        self.collection_calls: list[list[dict]] = []

    def restful(self, path, method="GET", json=None, **_kwargs):
        assert path == "composite/sobjects" and method == "PATCH"
        records = json["records"]
        self.collection_calls.append(records)
        out = []
        for rec in records:
            if rec["id"] in self.fail_ids and "Site_Type__c" in rec:
                out.append({"id": rec["id"], "success": False, "errors": [
                    {"statusCode": "DUPLICATES_DETECTED", "message": "dup"}]})
            else:
                out.append({"id": rec["id"], "success": True, "errors": []})
        return out


class _BatchClient:
    def __init__(self, fail_ids=()):
        self.sf = _BatchSF(fail_ids)


def _tower_row(sf_id):
    return {
        "Id": sf_id,
        "naip_site_type": "tower",
        "update_lat": 43.0,
        "update_lng": -89.0,
        "update_site_type": "Monopole",
        "update_verified_site": True,
        "update_verified_site_source": "FCC",
    }


class BatchApplyTests(unittest.TestCase):
    def test_collections_batch_and_holdout_fallback(self):
        from enrichment.sf_ops import apply_updates_batch

        client = _BatchClient(fail_ids={"a02"})
        rows = [_tower_row(f"a0{i}") for i in range(4)] + [
            {"Id": "a09", "naip_site_type": "other"}  # holdout dequeue
        ]
        entries = apply_updates_batch(client, rows, verbose=False)
        by_id = {e["Id"]: e for e in entries}
        self.assertEqual(by_id["a00"]["status"], "updated")
        self.assertEqual(by_id["a02"]["status"], "updated_holdout_after_error")
        self.assertEqual(
            by_id["a02"]["payload"], {"LLM_Classified__c": False, "LLM_Holdout__c": True}
        )
        self.assertEqual(by_id["a09"]["payload"]["LLM_Holdout__c"], True)
        # One collection for the five rows, one single PATCH for the retry.
        self.assertEqual(len(client.sf.collection_calls), 1)
        self.assertEqual(client.sf.Site__c.calls, [
            ("a02", {"LLM_Classified__c": False, "LLM_Holdout__c": True})
        ])

    def test_single_row_uses_plain_patch(self):
        from enrichment.sf_ops import apply_updates_batch

        client = _BatchClient()
        entries = apply_updates_batch(client, [_tower_row("a01")], verbose=False)
        self.assertEqual(entries[0]["status"], "updated")
        self.assertEqual(client.sf.collection_calls, [])
        self.assertEqual(len(client.sf.Site__c.calls), 1)

    def test_whole_call_failure_falls_back_row_by_row(self):
        from enrichment.sf_ops import apply_updates_batch

        client = _BatchClient()
        client.sf.restful = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503"))
        entries = apply_updates_batch(
            client, [_tower_row("a01"), _tower_row("a02")], verbose=False
        )
        self.assertEqual([e["status"] for e in entries], ["updated", "updated"])
        self.assertEqual(len(client.sf.Site__c.calls), 2)

    def test_dry_run_sends_nothing(self):
        from enrichment.sf_ops import apply_updates_batch

        client = _BatchClient()
        entries = apply_updates_batch(
            client, [_tower_row("a01"), _tower_row("a02")], dry_run=True, verbose=False
        )
        self.assertEqual([e["status"] for e in entries], ["dry_run", "dry_run"])
        self.assertEqual(client.sf.collection_calls, [])
        self.assertEqual(client.sf.Site__c.calls, [])


class _NoSql:
    def cursor(self):
        return SqlEmulatorCursor()

    def close(self):
        pass


def _rooftop_ok():
    return {
        "site_type": "rooftop",
        "site_confidence": 0.9,
        "cell_equipment": True,
        "cell_equipment_confidence": 0.9,
        "cell_equipment_evidence": "North oblique shows sector panel antennas",
        "cell_gear_kind": "sector_panel",
        "cell_models_agree": True,
        "dual_model_resolution": "agree_crop",
        "escalation_model": "claude",
        "asset_lat": 43.0005,
        "asset_lon": -89.0005,
        "asset_offset_m": 20.0,
        "asset_box_2d": "[220, 310, 360, 420]",
        "asset_view": "Nearmap oblique (North)",
        "nearmap_tier": "full",
        "nearmap_views": "Vert,North,East,South,West",
    }


class ParallelRunTests(unittest.TestCase):
    def _run(self, sites, classify, *, workers, apply=True, env=None):
        from enrichment.pipeline import run_enrichment
        import enrichment.metrics as metrics_mod

        client = _BatchClient()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        run_dir = root / "run"
        old_dir = metrics_mod.metrics_dir
        metrics_mod.metrics_dir = lambda: root
        environ = {"METRICS_SQL": "0", **(env or {})}
        try:
            with patch.dict(os.environ, environ):
                summary = run_enrichment(
                    sf_client=client,
                    sql_connection=_NoSql(),
                    run_dir=run_dir,
                    sites=sites,
                    classify_fn=classify,
                    apply=apply,
                    dequeue_holdouts=False,
                    verbose=False,
                    workers=workers,
                )
        finally:
            metrics_mod.metrics_dir = old_dir
        with (run_dir / DETAIL_CSV).open(newline="", encoding="utf-8") as handle:
            detail = list(csv.DictReader(handle))
        ledger = [
            json.loads(line)
            for line in (root / "sites.jsonl").read_text(encoding="utf-8").splitlines()
        ] if (root / "sites.jsonl").is_file() else []
        return client, summary, detail, ledger

    def test_parallel_workers_apply_every_site_once(self):
        threads: set[str] = set()
        lock = threading.Lock()

        def classify(**_kwargs):
            with lock:
                threads.add(threading.current_thread().name)
            time.sleep(0.02)
            return _rooftop_ok()

        sites = [
            {"Id": f"a0ZPAR{i:012d}", "Site_Latitude__c": 40.0 + i, "Site_Longitude__c": -89.0}
            for i in range(8)
        ]
        client, summary, detail, ledger = self._run(sites, classify, workers=4)
        written = [call[0] for call in client.sf.Site__c.calls] + [
            rec["id"] for call in client.sf.collection_calls for rec in call
        ]
        self.assertEqual(sorted(written), sorted(s["Id"] for s in sites))
        self.assertEqual(summary["apply"]["success"], 8)
        self.assertEqual(len(detail), 8)
        self.assertTrue(all(r["sf_update_status"] == "updated" for r in detail))
        self.assertGreater(len(threads), 1)
        self.assertEqual(len(ledger), 8)
        self.assertEqual(summary["kpis"]["unique_sites"], 8)
        self.assertEqual(summary["kpis"]["written_sites"], 8)

    def test_nearby_pins_reuse_cluster_result_in_parallel(self):
        calls = {"n": 0}
        lock = threading.Lock()

        def classify(**_kwargs):
            with lock:
                calls["n"] += 1
            return {"site_type": "other", "site_confidence": 0.95}

        sites = [
            {"Id": "a0ZNEAR000000001", "Site_Latitude__c": 43.0, "Site_Longitude__c": -89.0},
            {"Id": "a0ZNEAR000000002", "Site_Latitude__c": 43.00005, "Site_Longitude__c": -89.0},
            {"Id": "a0ZFAR0000000003", "Site_Latitude__c": 45.0, "Site_Longitude__c": -89.0},
        ]
        _client, _summary, detail, _ledger = self._run(sites, classify, workers=3)
        self.assertEqual(calls["n"], 2)
        stages = {r["Id"]: r["classification_stage"] for r in detail}
        self.assertEqual(stages["a0ZNEAR000000002"], "cluster_reuse")

    def test_batched_apply_size_uses_collections(self):
        sites = [
            {"Id": f"a0ZBAT{i:012d}", "Site_Latitude__c": 30.0 + i, "Site_Longitude__c": -89.0}
            for i in range(5)
        ]
        client, summary, detail, _ledger = self._run(
            sites, lambda **_k: _rooftop_ok(), workers=1, env={"APPLY_BATCH_SIZE": "2"}
        )
        self.assertEqual(summary["apply"]["success"], 5)
        self.assertTrue(client.sf.collection_calls)
        self.assertEqual(len(detail), 5)

    def test_default_batches_writes_and_csv_holds_unsent_sites(self):
        from enrichment import pipeline

        seen_pending: list[str] = []
        real_apply = pipeline._apply_rows

        def spy(sf_client, rows, *, run_dir, **kwargs):
            # Before a batch is sent, its sites are already in the detail CSV.
            with (run_dir / DETAIL_CSV).open(newline="", encoding="utf-8") as handle:
                on_disk = {r["Id"]: r["sf_update_status"] for r in csv.DictReader(handle)}
            seen_pending.extend(r["Id"] for r in rows if on_disk.get(r["Id"]) == "pending")
            return real_apply(sf_client, rows, run_dir=run_dir, **kwargs)

        sites = [
            {"Id": f"a0ZDEF{i:012d}", "Site_Latitude__c": 30.0 + i, "Site_Longitude__c": -89.0}
            for i in range(5)
        ]
        with patch.object(pipeline, "_apply_rows", side_effect=spy):
            client, summary, detail, _ledger = self._run(
                sites, lambda **_k: _rooftop_ok(), workers=1
            )
        self.assertEqual(len(client.sf.collection_calls), 1)
        self.assertEqual(len(client.sf.collection_calls[0]), 5)
        self.assertEqual(client.sf.Site__c.calls, [])
        self.assertEqual(sorted(seen_pending), sorted(s["Id"] for s in sites))
        self.assertTrue(all(r["sf_update_status"] == "updated" for r in detail))
        self.assertEqual(summary["apply"]["success"], 5)

    def test_misses_and_holdouts_count_as_unique_sites(self):
        def classify(**kwargs):
            if kwargs["site_id"].endswith("1"):
                return _rooftop_ok()
            return {"site_type": "other", "site_confidence": 0.4}

        sites = [
            {"Id": f"a0ZMIX00000000{i}", "Site_Latitude__c": 35.0 + i, "Site_Longitude__c": -89.0}
            for i in range(1, 4)
        ] + [{"Id": "a0ZNOCOORDS00001"}]
        _client, summary, _detail, ledger = self._run(sites, classify, workers=2)
        self.assertEqual(len(ledger), 4)
        self.assertEqual(summary["kpis"]["unique_sites"], 4)
        self.assertEqual(summary["kpis"]["written_sites"], 1)
        self.assertEqual(summary["kpis"]["errors"], 1)

    def test_dry_run_leaves_cumulative_ledger_alone(self):
        sites = [{"Id": "a0ZDRY0000000001", "Site_Latitude__c": 43.0, "Site_Longitude__c": -89.0}]
        client, summary, detail, ledger = self._run(
            sites, lambda **_k: _rooftop_ok(), workers=1, apply=False
        )
        self.assertEqual(ledger, [])
        self.assertIsNone(summary["kpis"])
        self.assertEqual(detail[0]["sf_update_status"], "dry_run")
        self.assertEqual(client.sf.Site__c.calls, [])


class OutcomeClassTests(unittest.TestCase):
    def test_new_outcome_labels(self):
        from enrichment.metrics import outcome_class

        self.assertEqual(
            outcome_class({"bucket": "potential_update", "naip_site_type": "tower",
                           "sf_update_status": "dequeued"}),
            "apply_failed",
        )
        self.assertEqual(
            outcome_class({"holdout_reason": "skip_classify_db_hit",
                           "sf_update_status": "updated"}),
            "applied_db_skip",
        )
        self.assertEqual(
            outcome_class({"holdout_reason": "db_only_no_unique_hit"}), "db_only_miss"
        )
        self.assertEqual(outcome_class({"holdout_reason": "no_saved_chips"}), "skipped")
        self.assertEqual(outcome_class({"holdout_reason": "no_imagery"}), "holdout_no_imagery")
        self.assertEqual(outcome_class({"holdout_reason": "sql_error"}), "error")


class WriteGroupingTests(unittest.TestCase):
    def test_db_matched_towers_count_as_tower_writes(self):
        from enrichment.metrics import rollup_kpis

        kpis = rollup_kpis([
            {"Id": "img", "outcome": "applied_tower", "final_site_type": "tower",
             "sf_update_status": "updated"},
            {"Id": "db", "outcome": "applied_db_skip", "final_site_type": "tower",
             "sf_update_status": "updated"},
            {"Id": "dbroof", "outcome": "applied_db_skip", "final_site_type": "rooftop",
             "sf_update_status": "updated"},
            {"Id": "roof", "outcome": "applied_rooftop", "final_site_type": "rooftop",
             "sf_update_status": "updated"},
        ])
        self.assertEqual(kpis["written_sites"], 4)
        self.assertEqual(kpis["tower_sf_writes"], 2)
        self.assertEqual(kpis["rooftop_sf_writes"], 2)
        self.assertEqual(kpis["db_skip_sf_writes"], 2)
        self.assertEqual(kpis["tower_write_rate"], 0.5)
        self.assertEqual(kpis["db_match_rate"], 0.5)


class MetricsSinkTests(unittest.TestCase):
    def test_sink_upserts_header_then_each_site(self):
        from enrichment import metrics_store

        executed: list[tuple[str, tuple]] = []

        class Cursor:
            def execute(self, sql, *params):
                if len(params) == 1 and isinstance(params[0], tuple):
                    params = params[0]
                executed.append((" ".join(sql.split()), params))

        class Conn:
            def cursor(self):
                return Cursor()

            def commit(self):
                pass

            def close(self):
                pass

        with patch("enrichment.mssql.connect_mssql", return_value=Conn()):
            sink = metrics_store.SiteSink("run-1", enabled=True)
            sink.begin({"sites": 2})
            sink.add([{"Id": "a0Z1", "outcome": "applied_tower"}])
            sink.close()
        merges = [(sql, p) for sql, p in executed if sql.startswith("MERGE")]
        self.assertTrue(merges[0][0].startswith("MERGE dbo.EnrichmentRun"))
        self.assertTrue(merges[1][0].startswith("MERGE dbo.EnrichmentSiteOutcome"))
        self.assertEqual(len(merges[1][1]), len(metrics_store.SITE_COLUMNS))
        self.assertEqual(merges[1][1][1], "a0Z1")

    def test_sink_goes_quiet_after_failure(self):
        from enrichment import metrics_store

        with patch("enrichment.mssql.connect_mssql", side_effect=RuntimeError("down")):
            sink = metrics_store.SiteSink("run-1", enabled=True)
            sink.begin({"sites": 1})
            self.assertFalse(sink.enabled)
            sink.add([{"Id": "a0Z1"}])  # no raise


class GeocodeBatchTests(unittest.TestCase):
    def test_parse_census_batch_csv(self):
        from enrichment.geo import parse_census_batch_csv

        text = (
            '"0","1598 Cleveland Pl, Denver, CO, 80202","Match","Exact",'
            '"1598 CLEVELAND PL, DENVER, CO, 80202","-104.9879,39.7417","123","L"\n'
            '"1","nowhere","No_Match"\n'
        )
        parsed = parse_census_batch_csv(text)
        self.assertAlmostEqual(parsed["0"]["lat"], 39.7417)
        self.assertAlmostEqual(parsed["0"]["lng"], -104.9879)
        self.assertEqual(parsed["0"]["source"], "census")
        self.assertIsNone(parsed["1"])

    def test_geocode_sites_uses_cache_then_one_batch(self):
        from enrichment import geo

        with tempfile.TemporaryDirectory() as tmp:
            cache = geo._GeocodeCache(Path(tmp) / "g.jsonl")
            cache.put("1 A St, X, CO 1", {"lat": 1.0, "lng": 2.0, "matched": "", "source": "census"})
            sites = [
                {"Site_Street__c": "1 A St", "Site_City__c": "X", "Site_State__c": "CO",
                 "Site_Zip_Code__c": "1"},
                {"Site_Street__c": "2 B St", "Site_City__c": "X", "Site_State__c": "CO",
                 "Site_Zip_Code__c": "1"},
            ]
            batches = []

            def fake_batch(rows):
                batches.append(rows)
                return {"0": {"lat": 3.0, "lng": 4.0, "matched": "", "source": "census"}}

            with patch.object(geo, "_geocode_cache", return_value=cache), patch.object(
                geo, "_fetch_census_batch", side_effect=fake_batch
            ):
                results = geo.geocode_sites(sites)
            reloaded = geo._GeocodeCache(Path(tmp) / "g.jsonl")
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 1)
        self.assertEqual(results["1 A St, X, CO 1"]["lat"], 1.0)
        self.assertEqual(results["2 B St, X, CO 1"]["lat"], 3.0)
        self.assertEqual(reloaded.get("2 B St, X, CO 1")[1]["lng"], 4.0)


if __name__ == "__main__":
    unittest.main()
