"""Shared Nearmap budget ledger + Gemini fallback model (offline, no APIs)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
MB = 1024 * 1024


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class _Now:
    def __init__(self, when: datetime) -> None:
        self.when = when

    def __call__(self) -> datetime:
        return self.when


def _budget(root, *, limit_mb=10, soft_pct=50, clock=None, now=None, refresh_s=5.0):
    from classifier import imagery

    return imagery.NearmapBudget(
        limit_mb, soft_pct, shared=True, root=root, refresh_s=refresh_s,
        clock=clock or _Clock(), now=now or _Now(datetime(2026, 10, 7, 12, tzinfo=timezone.utc)),
    )


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class SharedBudgetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_two_processes_see_each_others_purchases_after_refresh(self):
        from enrichment.metrics import NEARMAP_PURCHASES_JSONL

        clock = _Clock()
        a = _budget(self.root, clock=clock)
        b = _budget(self.root, clock=clock)
        self.assertEqual(a.spent, 0)
        self.assertEqual(b.spent, 0)
        a.add(3 * MB, purpose="pack", site_id="a0Z1", tiles=12)
        self.assertEqual(a.spent, 3 * MB)          # own purchase visible at once
        self.assertEqual(b.spent, 0)               # b's snapshot is still fresh
        clock.t += 5.1
        self.assertEqual(b.spent, 3 * MB)          # b refreshed from disk
        self.assertEqual(a.spent, 3 * MB)          # a reads its own row back: no double count
        b.add(2 * MB, purpose="wide")
        self.assertEqual(b.refresh(), 5 * MB)
        self.assertEqual(a.refresh(), 5 * MB)
        rows = _rows(self.root / NEARMAP_PURCHASES_JSONL)
        self.assertEqual([r["bytes"] for r in rows], [3 * MB, 2 * MB])
        self.assertEqual(rows[0]["site_id"], "a0Z1")
        self.assertEqual(rows[0]["tiles"], 12)
        self.assertEqual(rows[0]["pid"], os.getpid())
        self.assertNotEqual(rows[0]["proc"], rows[1]["proc"])
        self.assertTrue({"at", "purpose", "run_id"} <= set(rows[0]))

    def test_legacy_usage_rows_count_once_during_transition(self):
        from enrichment import metrics

        metrics._rewrite_jsonl(self.root / metrics.NEARMAP_USAGE_JSONL, [
            {"at": "2026-09-30T23:00:00Z", "bytes": 9 * MB},          # last month
            {"at": "2026-10-02T10:00:00Z", "bytes": 1 * MB},          # legacy (old code)
        ])
        a = _budget(self.root)
        self.assertEqual(a.spent, 1 * MB)
        a.add(2 * MB, purpose="pack")
        # The new-code site finishes: usage row flagged as covered by purchases.
        with patch.object(metrics, "metrics_dir", lambda: self.root):
            metrics.record_nearmap_usage("run-1", {"Id": "a0Z1", "nearmap_bytes": 2 * MB})
        # An old-code lane still running writes an unflagged row: still counted.
        metrics._append_jsonl(self.root / metrics.NEARMAP_USAGE_JSONL,
                              [{"at": "2026-10-07T11:00:00Z", "bytes": 4 * MB}])
        self.assertEqual(a.refresh(), 1 * MB + 2 * MB + 4 * MB)
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.assertEqual(metrics.shared_month_to_date_nearmap_bytes(now=now, root=self.root), 7 * MB)
        # Reporting figure keeps its meaning (all usage rows this month).
        self.assertEqual(metrics.month_to_date_nearmap_bytes(now=now, root=self.root), 7 * MB)

    def test_seed_keeps_prior_use_without_double_counting_the_ledger(self):
        from enrichment import metrics

        metrics._rewrite_jsonl(self.root / metrics.NEARMAP_USAGE_JSONL, [
            {"at": "2026-10-02T10:00:00Z", "bytes": 1 * MB},
            {"at": "2026-10-03T10:00:00Z", "bytes": 2 * MB, metrics.USAGE_IN_PURCHASES_KEY: True},
        ])
        metrics._rewrite_jsonl(self.root / metrics.NEARMAP_PURCHASES_JSONL, [
            {"at": "2026-10-03T09:59:00Z", "proc": "other", "bytes": 2 * MB, "purpose": "pack"},
        ])
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        a = _budget(self.root)
        # pipeline.py: seed(month_to_date_nearmap_bytes() + NEARMAP_PRIOR_USE_MB)
        a.seed(metrics.month_to_date_nearmap_bytes(now=now, root=self.root) + 50 * MB)
        self.assertEqual(a.spent, 1 * MB + 2 * MB + 50 * MB)
        a.set_prior_bytes(0)
        self.assertEqual(a.spent, 3 * MB)

    def test_month_rollover(self):
        now = _Now(datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc))
        clock = _Clock()
        a = _budget(self.root, now=now, clock=clock)
        a.seed(7 * MB)                      # prior use belongs to October only
        a.add(2 * MB)
        self.assertEqual(a.spent, 9 * MB)
        now.when = datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(a.spent, 0)
        a.add(1 * MB)
        clock.t += 10
        self.assertEqual(a.spent, 1 * MB)
        b = _budget(self.root, now=now)
        self.assertEqual(b.spent, 1 * MB)

    def test_soft_and_hard_stop_follow_the_shared_total(self):
        clock = _Clock()
        a = _budget(self.root, limit_mb=1, soft_pct=50, clock=clock)
        b = _budget(self.root, limit_mb=1, soft_pct=50, clock=clock)
        self.assertTrue(b.allows("wide"))
        a.add(int(0.6 * MB))
        clock.t += 6
        self.assertFalse(b.allows("wide"))
        self.assertFalse(b.allows("oblique_extra"))
        self.assertTrue(b.allows("pack"))
        a.add(1 * MB)
        clock.t += 6
        self.assertFalse(b.allows("pack"))
        self.assertIn("across all lanes", b.describe())

    def test_failed_ledger_write_still_counts_locally(self):
        from enrichment import metrics

        a = _budget(self.root)
        with patch.object(metrics, "record_nearmap_purchase", side_effect=OSError("disk full")):
            a.add(2 * MB)
        self.assertEqual(a.refresh(), 2 * MB)
        self.assertEqual(a.write_failures, 1)

    def test_local_budget_never_touches_disk(self):
        from classifier import imagery

        with patch.dict(os.environ, {"SITE_ORCHESTRATOR_DATA": str(self.root)}):
            local = imagery.NearmapBudget(1, 90)
            local.seed(5)
            local.add(7, purpose="pack")
            self.assertEqual(local.spent, 12)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(imagery.NearmapBudget.from_env().shared)

    def test_stitch_view_logs_each_purchase_with_site_id(self):
        import io as _io

        from PIL import Image

        from classifier import imagery
        from enrichment.metrics import NEARMAP_PURCHASES_JSONL

        buf = _io.BytesIO()
        Image.new("RGB", (256, 256), (1, 2, 3)).save(buf, format="JPEG")
        tile = buf.getvalue()
        shared = _budget(self.root, limit_mb=0)
        with patch.object(imagery, "BUDGET", shared), \
                patch.object(imagery, "_fetch_tile", return_value=(tile, False)):
            with imagery.nearmap_meter(site_id="a0ZSITE") as meter:
                imagery._stitch_view(43.0, -89.0, 30.0, "Vert", "2026-05-01", purpose="pack")
        rows = _rows(self.root / NEARMAP_PURCHASES_JSONL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bytes"], meter.bytes)
        self.assertEqual(rows[0]["site_id"], "a0ZSITE")
        self.assertEqual(rows[0]["purpose"], "pack")
        self.assertEqual(shared.refresh(), meter.bytes)  # budget off: still recorded


class LedgerAppendTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_concurrent_thread_appends_produce_valid_jsonl(self):
        from enrichment.metrics import NEARMAP_PURCHASES_JSONL, record_nearmap_purchase

        def worker(n: int) -> None:
            for i in range(60):
                record_nearmap_purchase(1000 + i, purpose="pack", proc=f"t{n}",
                                        site_id="x" * 200, root=self.root)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = _rows(self.root / NEARMAP_PURCHASES_JSONL)  # json.loads raises on a torn line
        self.assertEqual(len(rows), 8 * 60)
        self.assertEqual(sum(r["bytes"] for r in rows), 8 * sum(1000 + i for i in range(60)))

    def test_concurrent_process_appends_produce_valid_jsonl(self):
        from enrichment.metrics import NEARMAP_PURCHASES_JSONL

        code = (
            "import sys; from pathlib import Path; "
            "from enrichment.metrics import record_nearmap_purchase as r\n"
            "for i in range(40): r(10, purpose='pack', proc=sys.argv[2], "
            "site_id='y'*300, root=Path(sys.argv[1]))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        procs = [
            subprocess.Popen([sys.executable, "-c", code, str(self.root), f"p{n}"],
                             cwd=str(REPO_ROOT), env=env)
            for n in range(3)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0)
        rows = _rows(self.root / NEARMAP_PURCHASES_JSONL)
        self.assertEqual(len(rows), 3 * 40)
        self.assertEqual({r["proc"] for r in rows}, {"p0", "p1", "p2"})

    def test_tail_skips_a_half_written_line_until_it_completes(self):
        from enrichment.metrics import JsonlTail

        path = self.root / "x.jsonl"
        path.write_bytes(b'{"bytes": 1}\n{"bytes"')
        tail = JsonlTail(path)
        rows, reset = tail.read_new()
        self.assertEqual(rows, [{"bytes": 1}])
        self.assertFalse(reset)
        with path.open("ab") as handle:
            handle.write(b': 2}\n')
        self.assertEqual(tail.read_new()[0], [{"bytes": 2}])
        path.write_bytes(b'{"bytes": 3}\n')            # rewritten smaller -> reset
        rows, reset = tail.read_new()
        self.assertTrue(reset)
        self.assertEqual(rows, [{"bytes": 3}])


class BudgetCliTests(unittest.TestCase):
    def test_report_breaks_down_by_day_and_run(self):
        from enrichment import budget, metrics

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metrics._rewrite_jsonl(root / metrics.NEARMAP_PURCHASES_JSONL, [
                {"at": "2026-10-01T10:00:00Z", "run_id": "run-a", "purpose": "pack", "bytes": 2 * MB},
                {"at": "2026-10-02T10:00:00Z", "proc": "123-ab", "purpose": "wide", "bytes": 1 * MB},
                {"at": "2026-09-30T10:00:00Z", "run_id": "old", "purpose": "pack", "bytes": 9 * MB},
            ])
            metrics._rewrite_jsonl(root / metrics.NEARMAP_USAGE_JSONL, [
                {"at": "2026-10-01T09:00:00Z", "bytes": 1 * MB},
            ])
            env = {"NEARMAP_MONTHLY_BUDGET_MB": "10", "NEARMAP_BUDGET_SOFT_PCT": "50",
                   "NEARMAP_PRIOR_USE_MB": "0"}
            with patch.dict(os.environ, env):
                rep = budget.budget_report(month="2026-10", root=root)
                text = "\n".join(budget.format_report(rep))
        self.assertEqual(rep["total_bytes"], 4 * MB)
        self.assertEqual(rep["limit_bytes"], 10 * MB)
        self.assertEqual(rep["soft_stop_bytes"], 5 * MB)
        self.assertEqual(rep["by_day"], {"2026-10-01": 2 * MB, "2026-10-02": 1 * MB})
        self.assertEqual(set(rep["by_run"]), {"run-a", "pid 123-ab"})
        self.assertIn("Soft stop", text)


# ------------------------------ Gemini fallback ------------------------------


class _Overloaded(Exception):
    def __init__(self, code: int = 503, msg: str = "503 UNAVAILABLE. high demand") -> None:
        super().__init__(msg)
        self.code = code


class GeminiStatusTests(unittest.TestCase):
    def test_status_from_messages_and_attributes(self):
        from classifier.llm import _gemini_http_status, is_gemini_overload

        self.assertEqual(_gemini_http_status(Exception(
            "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently "
            "experiencing high demand.'}}")), 503)
        self.assertEqual(_gemini_http_status(Exception("Server error 503: overloaded")), 503)
        self.assertEqual(_gemini_http_status(Exception("UNAVAILABLE: try later")), 503)
        self.assertEqual(_gemini_http_status(Exception("429 RESOURCE_EXHAUSTED")), 429)
        self.assertEqual(_gemini_http_status(Exception("got 429 too many")), 429)
        self.assertEqual(_gemini_http_status(Exception("Quota RESOURCE_EXHAUSTED")), 429)
        self.assertEqual(_gemini_http_status(_Overloaded(503, "boom")), 503)
        self.assertEqual(_gemini_http_status(_Overloaded(429, "boom")), 429)
        self.assertIsNone(_gemini_http_status(Exception("400 INVALID_ARGUMENT")))
        self.assertIsNone(_gemini_http_status(Exception("site 15030 not found")))
        self.assertIsNone(_gemini_http_status(Exception("service temporarily unavailable")))
        self.assertTrue(is_gemini_overload(Exception("503 UNAVAILABLE")))
        self.assertFalse(is_gemini_overload(ValueError("bad json")))


class GeminiFallbackTests(unittest.TestCase):
    PRIMARY = "gemini-3-flash-preview"
    FALLBACK = "gemini-2.5-flash"

    def setUp(self):
        from classifier import llm

        self.clock = _Clock()
        patches = [
            patch.object(llm, "GEMINI_FALLBACK_BREAKER", llm.GeminiFallbackBreaker(clock=self.clock)),
            patch.dict(os.environ, {
                "GEMINI_FALLBACK_MODEL": self.FALLBACK,
                "GEMINI_FALLBACK_RETRIES": "3",
                "GEMINI_FALLBACK_AFTER": "2",
                "GEMINI_FALLBACK_COOLDOWN_S": "300",
            }),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.calls: list[tuple[str, object, object]] = []
        self.primary_error: Exception | None = _Overloaded()

    def _fake(self, client, contents, *, model, config, retries=None):
        self.calls.append((model, config, retries))
        if model == self.PRIMARY and self.primary_error is not None:
            raise self.primary_error
        return {"site_type": "tower", "site_confidence": 0.9}

    def _call(self):
        from classifier import asset_classifier as ac

        with patch.object(ac.llm, "call_gemini_json", side_effect=self._fake):
            return ac._call_gemini_json(object(), ["x"], {"type": "object"}, None, model=self.PRIMARY)

    def test_final_503_falls_back_with_the_fallback_models_config(self):
        with self.assertLogs("classifier.llm", level="WARNING") as logs:
            res = self._call()
        self.assertEqual(res["model"], self.FALLBACK)
        self.assertTrue(res["model_fallback"])
        self.assertEqual([c[0] for c in self.calls], [self.PRIMARY, self.FALLBACK])
        primary_cfg, fallback_cfg = self.calls[0][1], self.calls[1][1]
        self.assertIsNotNone(primary_cfg.thinking_config.thinking_level)   # Gemini 3.x
        self.assertIsNone(fallback_cfg.thinking_config.thinking_level)     # 2.5: budget
        self.assertIsNotNone(fallback_cfg.thinking_config.thinking_budget)
        self.assertEqual(self.calls[1][2], 3)                               # GEMINI_FALLBACK_RETRIES
        self.assertTrue(any(self.FALLBACK in line for line in logs.output))

    def test_final_429_message_only_also_falls_back(self):
        self.primary_error = Exception("429 RESOURCE_EXHAUSTED quota")
        self.assertEqual(self._call()["model"], self.FALLBACK)

    def test_other_errors_do_not_fall_back(self):
        self.primary_error = ValueError("400 INVALID_ARGUMENT schema")
        with self.assertRaises(ValueError):
            self._call()
        self.assertEqual([c[0] for c in self.calls], [self.PRIMARY])

    def test_no_fallback_configured_or_same_model_raises(self):
        for value in ("", self.PRIMARY):
            self.calls.clear()
            with patch.dict(os.environ, {"GEMINI_FALLBACK_MODEL": value}):
                with self.assertRaises(_Overloaded):
                    self._call()
            self.assertEqual([c[0] for c in self.calls], [self.PRIMARY])

    def test_primary_success_is_unchanged(self):
        self.primary_error = None
        res = self._call()
        self.assertEqual(res["model"], self.PRIMARY)
        self.assertNotIn("model_fallback", res)

    def test_breaker_opens_after_n_failures_and_probes_after_cooldown(self):
        self._call()
        self._call()                                 # 2nd consecutive failure: opens
        self.assertEqual([c[0] for c in self.calls],
                         [self.PRIMARY, self.FALLBACK, self.PRIMARY, self.FALLBACK])
        self.calls.clear()
        self.clock.t += 100
        res = self._call()                           # open: straight to fallback
        self.assertEqual([c[0] for c in self.calls], [self.FALLBACK])
        self.assertTrue(res["model_fallback"])
        self.calls.clear()
        self.clock.t += 201                          # cooldown over: probe, still down
        self._call()
        self.assertEqual([c[0] for c in self.calls], [self.PRIMARY, self.FALLBACK])
        self.calls.clear()
        self.clock.t += 10                           # re-opened by the failed probe
        self._call()
        self.assertEqual([c[0] for c in self.calls], [self.FALLBACK])
        self.calls.clear()
        self.clock.t += 301
        self.primary_error = None                    # primary recovered
        res = self._call()
        self.assertEqual(res["model"], self.PRIMARY)
        self.calls.clear()
        self._call()                                 # closed again
        self.assertEqual([c[0] for c in self.calls], [self.PRIMARY])

    def test_success_resets_the_consecutive_count(self):
        self._call()                                 # 1 failure
        self.primary_error = None
        self._call()                                 # success resets
        self.primary_error = _Overloaded()
        self._call()                                 # 1 failure again: still closed
        self.calls.clear()
        self.primary_error = None
        self._call()
        self.assertEqual([c[0] for c in self.calls], [self.PRIMARY])

    def test_only_one_probe_at_a_time(self):
        from classifier import llm

        breaker = llm.GEMINI_FALLBACK_BREAKER
        breaker.record_overload(self.PRIMARY, after=1, cooldown_s=10, fallback=self.FALLBACK)
        self.assertEqual(breaker.route(self.PRIMARY), breaker.FALLBACK)
        self.clock.t += 11
        routes = []
        threads = [threading.Thread(target=lambda: routes.append(breaker.route(self.PRIMARY)))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(routes.count(breaker.PROBE), 1)
        self.assertEqual(routes.count(breaker.FALLBACK), 7)


if __name__ == "__main__":
    unittest.main()
