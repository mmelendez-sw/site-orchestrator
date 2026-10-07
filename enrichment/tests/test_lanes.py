"""Lane runner: planning, env, stops, flush, status (offline, fake launcher)."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from enrichment import lanes
from enrichment.lanes import (
    Batch,
    LaneRunner,
    aggregate_status,
    build_batch_env,
    build_plan,
    count_rate_limit_lines,
    flush_env,
    order_queue,
    parse_env_pairs,
    read_ids_file,
    run_suffix,
    split_lanes,
    split_rpm,
    subtract_ids,
)

DETAIL_FIELDS = ["Id", "audit_verdict", "audit_reason", "update_site_type", "sf_update_status", "bucket"]


def write_detail(run_dir: Path, rows: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "enrichment_detail.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAIL_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in DETAIL_FIELDS})


class FakeProc:
    _pid = 1000

    def __init__(self, env: dict, *, immediate: bool):
        FakeProc._pid += 1
        self.pid = FakeProc._pid
        self.env = env
        self.released = threading.Event()
        self.terminated = False
        self.returncode = None
        if immediate:
            self.release(0)

    def release(self, rc: int = 0) -> None:
        if self.returncode is None:
            self.returncode = rc
        self.released.set()

    def wait(self, timeout=None):
        if not self.released.wait(timeout if timeout is not None else 10):
            raise TimeoutError("fake proc still running")
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.release(-15)

    def kill(self):
        self.terminate()


class FakeLauncher:
    """Batches block until released; APPLY_EXISTING flushes finish at once."""

    def __init__(self, *, immediate: bool = False):
        self.immediate = immediate
        self.calls: list[tuple[list[str], dict]] = []
        self.procs: list[FakeProc] = []
        self.lock = threading.Lock()

    def __call__(self, cmd, *, env, cwd, stdout, stderr):
        proc = FakeProc(env, immediate=self.immediate or env.get("APPLY_EXISTING") == "1")
        with self.lock:
            self.calls.append((list(cmd), dict(env)))
            self.procs.append(proc)
        return proc

    def batch_calls(self):
        return [env for _cmd, env in self.calls if env.get("APPLY_EXISTING") != "1"]

    def flush_calls(self):
        return [env for _cmd, env in self.calls if env.get("APPLY_EXISTING") == "1"]

    def wait_for(self, n: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.procs) >= n:
                    return
            time.sleep(0.01)
        raise AssertionError(f"expected {n} launches, saw {len(self.procs)}")


def ids(n: int) -> list[str]:
    return [f"a0Z{i:015d}" for i in range(n)]


class PlanTests(unittest.TestCase):
    def test_lane_i_gets_chunks_i_plus_n(self):
        plan = split_lanes(ids(10), lanes=3, batch=2)
        self.assertEqual([[c for c, _ in lane] for lane in plan], [[0, 3], [1, 4], [2]])
        self.assertEqual(plan[0][1][1], ids(10)[6:8])
        self.assertEqual(sum(len(chunk) for lane in plan for _c, chunk in lane), 10)

    def test_build_plan_names_runs_with_audit_suffix(self):
        root = Path("runs")
        plan = build_plan(ids(5), lanes=2, batch=2, runs_root=root, lanes_dir=root / "x",
                          stamp="2026-10-07_120000", name="pool", suffix="_connectx_audit")
        self.assertEqual(plan[1][0].run_dir.name, "2026-10-07_120000_pool_L2_C2_connectx_audit")
        self.assertEqual(plan[0][1].run_dir.name, "2026-10-07_120000_pool_L1_C3_connectx_audit")
        self.assertEqual(plan[0][1].ids, [ids(5)[4]])
        self.assertTrue(all(b.run_dir.name.endswith("_connectx_audit") for lane in plan for b in lane))

    def test_rpm_split_evenly_min_one(self):
        self.assertEqual(split_rpm(60, 5), 12)
        self.assertEqual(split_rpm(50, 4), 12)
        self.assertEqual(split_rpm(3, 5), 1)
        self.assertEqual(split_rpm(0, 2), 1)

    def test_run_suffix(self):
        self.assertEqual(run_suffix({"CONNECTX_AUDIT": "1"}), "_connectx_audit")
        self.assertEqual(run_suffix({"CONNECTX_AUDIT": "1", "APPLY": "0"}), "_connectx_audit_dryrun")
        self.assertEqual(run_suffix({}), "_lanes")

    def test_queue_order_and_subtract(self):
        q = ["A00000000000001xyz", "A00000000000002xyz", "A00000000000003xyz"]
        tiers = {"A00000000000003": 1, "A00000000000001": 2}
        self.assertEqual(order_queue(q, tiers), [q[2], q[0], q[1]])
        self.assertEqual(order_queue(q, None), q)
        self.assertEqual(subtract_ids(q + [q[0]], ["A00000000000002"]), [q[0], q[2]])

    def test_read_ids_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ids.txt"
            path.write_text("Id\nA1\r\n\n# note\nA2\nA1\n", encoding="utf-8")
            self.assertEqual(read_ids_file(path), ["A1", "A2"])


class EnvTests(unittest.TestCase):
    PARENT = {
        "PATH": "x", "LIMIT": "500", "IDS": "OLD", "APPLY_EXISTING": "1", "RUN_DIR": "old",
        "NEARMAP_MONTHLY_BUDGET_MB": "2750",
    }

    def _env(self, extra=None, **kw):
        args = dict(ids=["A", "B"], run_dir=Path("runs/r1"), batch_size=75, workers=4,
                    gemini_rpm=12, claude_rpm=10, extra=extra or {})
        args.update(kw)
        return build_batch_env(self.PARENT, **args)

    def test_batch_env_overrides_leftovers(self):
        env = self._env(lanes.lane_extra_env({}, pool_audit=True))
        self.assertEqual(env["LIMIT"], "75")
        self.assertEqual(env["IDS"], "A,B")
        self.assertEqual(env["RUN_DIR"], str(Path("runs/r1")))
        self.assertEqual(env["APPLY"], "1")
        self.assertNotIn("APPLY_EXISTING", env)
        self.assertEqual(env["NEARMAP_MONTHLY_BUDGET_MB"], "2750")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual((env["CLASSIFY_WORKERS"], env["GEMINI_RPM"], env["CLAUDE_RPM"]), ("4", "12", "10"))
        self.assertEqual((env["CONNECTX_AUDIT"], env["AUDIT_POOL"], env["STAGES"]),
                         ("1", "1", "New/Unreviewed"))

    def test_env_flag_overrides_apply_and_limit(self):
        extra = lanes.lane_extra_env(parse_env_pairs(["APPLY=0", "LIMIT=10"]), pool_audit=False)
        env = self._env(extra)
        self.assertEqual(env["APPLY"], "0")
        self.assertEqual(env["LIMIT"], "10")
        self.assertNotIn("CONNECTX_AUDIT", env)

    def test_reserved_env_keys_rejected(self):
        for bad in ("IDS=x", "RUN_DIR=x", "APPLY_EXISTING=1", "novalue"):
            with self.assertRaises(ValueError):
                parse_env_pairs([bad])

    def test_flush_env(self):
        env = flush_env(self.PARENT, Path("runs/r1"), {"CONNECTX_AUDIT": "1", "APPLY": "1", "LIMIT": "75"})
        self.assertEqual((env["CONNECTX_AUDIT"], env["APPLY"], env["APPLY_EXISTING"]), ("1", "1", "1"))
        self.assertEqual(env["RUN_DIR"], str(Path("runs/r1")))
        self.assertNotIn("IDS", env)
        self.assertNotIn("LIMIT", env)


class RateLimitTests(unittest.TestCase):
    def test_regex_ignores_coordinates_and_distances(self):
        text = "\n".join([
            "  -> SF pin: 42.247662, -84.408429",
            "  asset 429 m from pin",
            "WARNING Gemini 503 (service unavailable) — all workers pause 21s (retry 1/3)",
            "WARNING Gemini 429 quota",
            "google.genai RESOURCE_EXHAUSTED",
            "WARNING Claude 429 on cell — all workers pause 10s (retry 1/3)",
        ])
        self.assertEqual(count_rate_limit_lines(text), 4)
        self.assertEqual(count_rate_limit_lines("-84.408429\n429 m from pin\nHTTP 4291"), 0)


class StatusTests(unittest.TestCase):
    def test_aggregate_from_detail_csvs_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = build_plan(ids(6), lanes=2, batch=2, runs_root=root, lanes_dir=root / "lanes",
                              stamp="S", name="pool", suffix="_connectx_audit")
            b1, b2, b3 = plan[0][0], plan[1][0], plan[0][1]
            b1.state, b2.state = "done", "running"
            write_detail(b1.run_dir, [
                {"Id": "1", "audit_verdict": "confirmed", "update_site_type": "Rooftop", "sf_update_status": "updated"},
                {"Id": "2", "audit_verdict": "confirmed", "update_site_type": "Tower", "sf_update_status": "updated"},
            ])
            write_detail(b2.run_dir, [
                {"Id": "3", "audit_verdict": "no_asset", "sf_update_status": "pending"},
                {"Id": "4", "audit_verdict": "inconclusive", "audit_reason": "naip_only", "sf_update_status": ""},
            ])
            write_detail(b3.run_dir, [{"Id": "9", "audit_verdict": "confirmed"}])  # queued: ignored
            b1.log_path.parent.mkdir(parents=True, exist_ok=True)
            b1.log_path.write_text("SF pin: 1, -84.408429\nWARNING Gemini 503 — all workers pause\n"
                                   "Traceback (most recent call last):\n", encoding="utf-8")
            status = aggregate_status(plan)
        self.assertEqual(status["sites_done"], 4)
        self.assertEqual(status["verdicts"], {"confirmed rooftop": 1, "confirmed tower": 1,
                                              "no_asset": 1, "inconclusive": 1})
        self.assertEqual(status["inconclusive_reasons"], {"naip_only": 1})
        self.assertEqual(status["sf_update_status"]["updated"], 2)
        self.assertEqual(status["rate_limit_warnings"], 1)
        self.assertEqual(status["tracebacks"], 1)
        self.assertEqual([lane["batch"] for lane in status["lanes"]], [None, 2])
        self.assertEqual([lane["sites_done"] for lane in status["lanes"]], [2, 2])
        self.assertIn("confirmed rooftop", lanes.format_status({**status, "nearmap_mb": 12.0}))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lanes_dir = self.root / "lanes"
        self.lanes_dir.mkdir()
        self.plan = build_plan(ids(8), lanes=2, batch=2, runs_root=self.root, lanes_dir=self.lanes_dir,
                               stamp="S", name="pool", suffix="_connectx_audit")
        self.messages: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _runner(self, launcher, **kw) -> LaneRunner:
        return LaneRunner(
            plan=self.plan,
            env_for=lambda b: {"IDS": ",".join(b.ids), "RUN_DIR": str(b.run_dir)},
            flush_env_for=lambda run_dir: {"APPLY_EXISTING": "1", "RUN_DIR": str(run_dir)},
            lanes_dir=self.lanes_dir,
            launcher=launcher,
            out=self.messages.append,
            **kw,
        )

    def test_stop_at_mb_stops_new_launches_but_lets_running_finish(self):
        launcher = FakeLauncher()
        mb = {"v": 100.0}
        runner = self._runner(launcher, mb_fn=lambda: mb["v"], stop_at_mb=500)
        runner.check()
        runner.start()
        launcher.wait_for(2)
        mb["v"] = 600.0
        runner.check()
        for proc in list(launcher.procs):
            proc.release(0)
        runner.join(5)
        self.assertFalse(runner.alive())
        self.assertEqual(len(launcher.batch_calls()), 2)
        self.assertFalse(any(p.terminated for p in launcher.procs))
        states = {b.name: b.state for lane in self.plan for b in lane}
        self.assertEqual(states, {"L1_C1": "done", "L2_C2": "done", "L1_C3": "skipped", "L2_C4": "skipped"})
        self.assertIn("nearmap 600 MB", runner.stop_reason)

    def test_covered_terminates_and_flushes_only_unfinished_pending_runs(self):
        launcher = FakeLauncher()
        runner = self._runner(launcher, coverage_fn=lambda: (5, 5))
        runner.start()
        launcher.wait_for(2)
        b1, b2 = self.plan[0][0], self.plan[1][0]
        write_detail(b1.run_dir, [{"Id": "1", "sf_update_status": "pending"},
                                  {"Id": "2", "sf_update_status": "updated"}])
        write_detail(b2.run_dir, [{"Id": "3", "sf_update_status": "pending"}])
        (b2.run_dir / "summary.json").write_text("{}", encoding="utf-8")
        # A queued batch's leftover folder must not be flushed either.
        write_detail(self.plan[0][1].run_dir, [{"Id": "9", "sf_update_status": "pending"}])
        runner.check()
        runner.join(5)
        self.assertTrue(all(p.terminated for p in launcher.procs))
        self.assertEqual({b.state for lane in self.plan for b in lane}, {"killed", "skipped"})
        flushed = runner.flush()
        flushes = launcher.flush_calls()
        self.assertEqual([f["RUN_DIR"] for f in flushes], [str(b1.run_dir)])
        self.assertEqual(flushed, [{"run_dir": str(b1.run_dir), "pending": 1, "exit": 0}])

    def test_not_covered_keeps_running(self):
        launcher = FakeLauncher(immediate=True)
        runner = self._runner(launcher, coverage_fn=lambda: (3, 5))
        rc = runner.run(check_every_s=0, status_every_s=0, poll_s=0.01)
        self.assertEqual(rc, 0)
        self.assertEqual(len(launcher.batch_calls()), 4)
        self.assertEqual((runner.stock, runner.owed), (3, 5))
        status = json.loads((self.lanes_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual([b["state"] for b in status["batches"]], ["done"] * 4)

    def test_covered_before_start_launches_nothing(self):
        launcher = FakeLauncher(immediate=True)
        runner = self._runner(launcher, coverage_fn=lambda: (5, 4))
        runner.run(check_every_s=0, status_every_s=100, poll_s=0.01)
        self.assertEqual(launcher.calls, [])

    def test_failed_batch_exit_code(self):
        launcher = FakeLauncher()
        runner = self._runner(launcher)
        result: dict = {}
        thread = threading.Thread(target=lambda: result.setdefault(
            "rc", runner.run(check_every_s=100, status_every_s=100, poll_s=0.01)))
        thread.start()
        for n in (2, 3, 4):
            launcher.wait_for(n)
            launcher.procs[n - 2].release(1 if n == 2 else 0)
        for proc in launcher.procs:
            proc.release(0)
        thread.join(5)
        self.assertEqual(result["rc"], 1)


class CliTests(unittest.TestCase):
    def test_dry_run_launches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ids_file = Path(tmp) / "ids.txt"
            ids_file.write_text("\n".join(ids(7)), encoding="utf-8")
            launcher = FakeLauncher(immediate=True)
            with mock.patch.dict(os.environ, {"SITE_ORCHESTRATOR_DATA": tmp}), \
                    mock.patch("dotenv.load_dotenv"), \
                    mock.patch("builtins.print"):
                rc = lanes.main(["--ids-file", str(ids_file), "--lanes", "3", "--batch", "2",
                                 "--env", "CONNECTX_AUDIT=1", "--dry-run",
                                 "--gemini-rpm-total", "30", "--claude-rpm-total", "20"],
                                launcher=launcher)
            self.assertEqual(rc, 0)
            self.assertEqual(launcher.calls, [])
            self.assertFalse((Path(tmp) / "runs").exists())

    def test_bad_env_pair_exits_2(self):
        with mock.patch("dotenv.load_dotenv"), mock.patch("sys.stderr"):
            self.assertEqual(lanes.main(["--ids-file", "x", "--env", "IDS=1"]), 2)


class MonthToDateTests(unittest.TestCase):
    def test_prefers_shared_ledger_and_adds_prior_use(self):
        from enrichment import metrics

        with mock.patch.dict(os.environ, {"NEARMAP_PRIOR_USE_MB": "10"}), \
                mock.patch.object(metrics, "shared_month_to_date_nearmap_bytes",
                                  return_value=5 * lanes.MB, create=True), \
                mock.patch.object(metrics, "month_to_date_nearmap_bytes", return_value=999 * lanes.MB):
            self.assertAlmostEqual(lanes.month_to_date_mb(), 15.0)


class BatchTests(unittest.TestCase):
    def test_batch_name(self):
        self.assertEqual(Batch(lane=2, chunk=7, ids=[], run_dir=Path("r"), log_path=Path("l")).name, "L2_C7")


if __name__ == "__main__":
    unittest.main()
