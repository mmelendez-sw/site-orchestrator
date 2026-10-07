"""Run ``python -m enrichment`` as N parallel lanes of fixed-size batches.

python -m enrichment.lanes --pool-audit --lanes 5 --batch 75 --stop-when-covered
python -m enrichment.lanes --ids-file ids.txt --env CONNECTX_AUDIT=1 --dry-run

The queue (``--ids-file``, or ``--pool-audit``: the Site Acquisition Team's
ConnectX Rooftops in New/Unreviewed minus Ids an earlier live audit decided,
optionally ordered by ``--priority-csv`` Id,tier) is cut into batches of
``--batch`` Ids. Lane i runs chunks i, i+N, i+2N, ... one ``python -m
enrichment`` at a time, so the highest-priority chunks start first. Gemini /
Claude RPM totals are split evenly across lanes.

Each batch gets its own RUN_DIR (``runs/<stamp>_<name>_L<lane>_C<chunk>
_connectx_audit`` for an audit, so ``prior_audit_ids`` skips its Ids next
time) and a log in ``runs/<stamp>_lanes_<name>/`` next to plan.json and
status.json.

Stops: ``--stop-at-mb`` (month-to-date Nearmap) stops launching and lets
running batches finish; ``--stop-when-covered`` (confirmed pool rooftops >=
open owed rep slots) and Ctrl+C also terminate running batches. Every stop
ends with a flush: each batch RUN_DIR with ``pending`` rows and no
summary.json is pushed with ``APPLY_EXISTING=1`` so no finished site is lost.
A second Ctrl+C aborts the flush.

This module only reads Salesforce (queue building); writes happen in the
batch and flush subprocesses.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enrichment.connectx_audit import AUDIT_DRY_RUN_SUFFIX, AUDIT_RUN_SUFFIX  # noqa: E402
from enrichment.constants import DETAIL_CSV  # noqa: E402

SUMMARY_JSON = "summary.json"
PLAN_JSON = "plan.json"
STATUS_JSON = "status.json"
EVENTS_LOG = "events.log"
MB = 1024 * 1024

# Real model back-pressure lines only. A bare "429" also matches coordinates
# ("-84.408429") and distances ("429 m from pin").
RATE_LIMIT_RE = re.compile(r"Gemini 429|Gemini 503|all workers pause|RESOURCE_EXHAUSTED")
TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\)", re.MULTILINE)

POOL_AUDIT_ENV = {"CONNECTX_AUDIT": "1", "AUDIT_POOL": "1", "STAGES": "New/Unreviewed"}
# Lessons from the hand-run lanes: small apply batches (a killed batch loses
# little) and verbose logs to diagnose a lane.
LANE_DEFAULT_ENV = {"APPLY": "1", "APPLY_BATCH_SIZE": "10", "VERBOSE": "1"}
# Leftover shell values that would reshape or hijack a batch.
STRIPPED_PARENT_KEYS = (
    "IDS", "LIMIT", "OFFSET", "QUEUE_OFFSET", "RUN_DIR", "APPLY_EXISTING",
    "RERUN_SITES_FROM", "RERUN_HOLDOUTS_FROM",
)
# Set by the lane runner per batch; --env cannot override them.
RESERVED_KEYS = frozenset({"IDS", "RUN_DIR", "APPLY_EXISTING"})

TRUE_VALUES = frozenset({"1", "true", "yes"})

Launcher = Callable[..., Any]


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


# ---------------------------------------------------------------- planning


@dataclass
class Batch:
    lane: int  # 1-based
    chunk: int  # 1-based, global queue order
    ids: list[str]
    run_dir: Path
    log_path: Path
    state: str = "queued"  # queued | running | done | failed | killed | skipped
    returncode: int | None = None
    started: str = ""
    ended: str = ""

    @property
    def name(self) -> str:
        return f"L{self.lane}_C{self.chunk}"


def chunk_ids(ids: Sequence[str], batch: int) -> list[list[str]]:
    size = max(1, int(batch))
    return [list(ids[i:i + size]) for i in range(0, len(ids), size)]


def split_lanes(ids: Sequence[str], lanes: int, batch: int) -> list[list[tuple[int, list[str]]]]:
    """Lane i (0-based) gets chunks i, i+N, i+2N, ... as (chunk index, ids)."""
    n = max(1, int(lanes))
    chunks = chunk_ids(ids, batch)
    return [[(c, chunks[c]) for c in range(lane, len(chunks), n)] for lane in range(n)]


def split_rpm(total: float, lanes: int) -> int:
    """Per-lane share of a model RPM budget, at least 1."""
    return max(1, int(float(total) // max(1, int(lanes))))


def run_suffix(env: dict[str, str]) -> str:
    """Audit batches end with the audit suffix so prior_audit_ids sees them."""
    if _truthy(env.get("CONNECTX_AUDIT")):
        return AUDIT_RUN_SUFFIX if _truthy(env.get("APPLY", "1")) else AUDIT_DRY_RUN_SUFFIX
    return "_lanes"


def build_plan(
    ids: Sequence[str],
    *,
    lanes: int,
    batch: int,
    runs_root: Path,
    lanes_dir: Path,
    stamp: str,
    name: str,
    suffix: str,
) -> list[list[Batch]]:
    plan: list[list[Batch]] = []
    for lane_idx, chunks in enumerate(split_lanes(ids, lanes, batch)):
        lane_batches = []
        for c, chunk in chunks:
            tag = f"L{lane_idx + 1}_C{c + 1}"
            lane_batches.append(
                Batch(
                    lane=lane_idx + 1,
                    chunk=c + 1,
                    ids=chunk,
                    run_dir=runs_root / f"{stamp}_{name}_{tag}{suffix}",
                    log_path=lanes_dir / f"{tag}.log",
                )
            )
        plan.append(lane_batches)
    return plan


def parse_env_pairs(pairs: Iterable[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"--env expects KEY=VALUE, got {pair!r}")
        if key.upper() in RESERVED_KEYS:
            raise ValueError(f"--env {key} is set per batch by the lane runner")
        out[key] = value
    return out


def base_child_env(parent: dict[str, str]) -> dict[str, str]:
    """Parent environment minus leftover queue-shaping keys."""
    return {k: v for k, v in parent.items() if k.upper() not in STRIPPED_PARENT_KEYS}


def lane_extra_env(extra: dict[str, str], *, pool_audit: bool) -> dict[str, str]:
    """Defaults (pool audit, APPLY=1, ...) overlaid by the operator's --env."""
    merged = dict(LANE_DEFAULT_ENV)
    if pool_audit:
        merged.update(POOL_AUDIT_ENV)
    merged.update(extra)
    return merged


def build_batch_env(
    parent: dict[str, str],
    *,
    ids: Sequence[str],
    run_dir: Path,
    batch_size: int,
    workers: int,
    gemini_rpm: int,
    claude_rpm: int,
    extra: dict[str, str],
) -> dict[str, str]:
    """Child environment for one batch.

    Terminal env beats ``.env`` in the child (load_dotenv never overrides),
    so LIMIT here wins over the ``.env`` LIMIT=500. ``extra`` (defaults +
    --env) may override LIMIT / APPLY / RPM / workers; IDS and RUN_DIR are
    always the batch's own. NEARMAP_MONTHLY_BUDGET_MB passes through from
    the parent when set there.
    """
    env = base_child_env(parent)
    env.update(
        {
            "APPLY": "1",
            "LIMIT": str(batch_size),
            "CLASSIFY_WORKERS": str(workers),
            "GEMINI_RPM": str(gemini_rpm),
            "CLAUDE_RPM": str(claude_rpm),
        }
    )
    env.update(extra)
    env.update(
        {
            "IDS": ",".join(ids),
            "RUN_DIR": str(run_dir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.pop("APPLY_EXISTING", None)
    return env


def flush_env(parent: dict[str, str], run_dir: Path, extra: dict[str, str]) -> dict[str, str]:
    """``APPLY_EXISTING=1`` push of one interrupted batch's unsent rows."""
    env = base_child_env(parent)
    env.update({k: v for k, v in extra.items() if k not in {"LIMIT", "APPLY", "VERBOSE"}})
    env.update(
        {
            "APPLY": "1",
            "APPLY_EXISTING": "1",
            "RUN_DIR": str(run_dir),
            "VERBOSE": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if _truthy(extra.get("CONNECTX_AUDIT")):
        env["CONNECTX_AUDIT"] = "1"
    return env


# ------------------------------------------------------------------ queue


def read_ids_file(path: Path) -> list[str]:
    ids = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip().split(",")[0].strip()
        if value and not value.startswith("#") and value.lower() != "id":
            ids.append(value)
    return list(dict.fromkeys(ids))


def read_priority_csv(path: Path) -> dict[str, float]:
    tiers: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            sf_id = str(row.get("Id") or "").strip()
            try:
                tier = float(str(row.get("tier") or "").strip())
            except ValueError:
                continue
            if sf_id:
                tiers[sf_id[:15]] = tier
    return tiers


def order_queue(ids: Sequence[str], tiers: dict[str, float] | None) -> list[str]:
    """Lower tier first; untiered Ids last; queue order kept within a tier."""
    if not tiers:
        return list(ids)
    rank = {sf_id: i for i, sf_id in enumerate(ids)}
    return sorted(ids, key=lambda i: (tiers.get(i[:15], float("inf")), rank[i]))


def subtract_ids(ids: Sequence[str], drop: Iterable[str]) -> list[str]:
    gone = {str(d).strip()[:15] for d in drop if str(d).strip()}
    return [i for i in dict.fromkeys(ids) if i[:15] not in gone]


def build_pool_queue(sf_client, runs_root: Path) -> tuple[list[str], int, int]:
    """(queue, SF matches, prior-decided dropped). Read-only SOQL."""
    from enrichment.connectx_audit import build_connectx_audit_query, prior_audit_ids
    from enrichment.sf_ops import query_all

    soql = build_connectx_audit_query(pool=True, stages=["New/Unreviewed"], fields=("Id",))
    found = [str(r.get("Id") or "").strip() for r in query_all(sf_client, soql)]
    found = [i for i in found if i]
    queue = subtract_ids(found, prior_audit_ids(runs_root))
    return queue, len(found), len(found) - len(queue)


# --------------------------------------------------------------- monitors


def month_to_date_mb() -> float:
    """Month-to-date Nearmap MB as the batches' own budget sees it.

    Prefers the shared per-purchase ledger (all lanes, written as tiles are
    bought: ``metrics.shared_month_to_date_nearmap_bytes``) or an
    ``enrichment.budget`` helper when present; falls back to the per-site
    usage ledger. Adds ``NEARMAP_PRIOR_USE_MB`` like the pipeline does.
    """
    from enrichment import metrics
    from envutil import env_float

    prior = env_float("NEARMAP_PRIOR_USE_MB", 0)
    try:
        from enrichment import budget as shared  # type: ignore[attr-defined]
    except ImportError:
        shared = None
    for module, name, scale in (
        (shared, "month_to_date_mb", 1.0),
        (shared, "month_to_date_bytes", 1.0 / MB),
        (metrics, "shared_month_to_date_nearmap_bytes", 1.0 / MB),
        (metrics, "month_to_date_nearmap_bytes", 1.0 / MB),
    ):
        fn = getattr(module, name, None) if module is not None else None
        if callable(fn):
            return float(fn()) * scale + prior
    return prior


def coverage_counts(runs_root: Path | None = None) -> tuple[int, int]:
    """(stock, open owed) as reconcile_swaps would see them right now.

    Stock: confirmed pool Rooftops, written or still pending (pending rows are
    flushed on stop), not already handed out. Owed: pull-ledger slots plus
    rep audit losses, minus slots already filled.
    """
    from enrichment.connectx_audit import iter_audit_rows, site_acq_owner_id
    from enrichment.reconcile_pull import PULL_LEDGER_CSV, read_pull_ledger
    from enrichment.reconcile_swaps import (
        LEDGER_CSV,
        collect_losses,
        collect_stock,
        losses_from_pull_ledger,
        read_ledger,
        swaps_dir,
    )
    from paths import runs_dir

    pool = site_acq_owner_id()
    rows = list(iter_audit_rows(runs_root or runs_dir()))
    for _run, _live, row in rows:
        if str(row.get("sf_update_status") or "").strip().lower() == "pending":
            row["sf_update_status"] = "updated"
    filled = [r for r in read_ledger(swaps_dir() / LEDGER_CSV) if r.get("status") == "applied"]
    used_losses = {r["lost_id"] for r in filled}
    used_stock = {r["replacement_id"] for r in filled}
    owed = losses_from_pull_ledger(read_pull_ledger(swaps_dir() / PULL_LEDGER_CSV))
    pulled = {loss.lost_id for loss in owed}
    owed += [loss for loss in collect_losses(rows, pool) if loss.lost_id not in pulled]
    open_owed = sum(1 for loss in owed if loss.lost_id not in used_losses)
    stock = sum(1 for s in collect_stock(rows, pool) if s.site_id not in used_stock)
    return stock, open_owed


def count_rate_limit_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if RATE_LIMIT_RE.search(line))


def read_detail(run_dir: Path) -> list[dict[str, str]]:
    path = run_dir / DETAIL_CSV
    if not path.is_file():
        return []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []  # being rewritten by the batch; next tick will see it


def pending_rows(run_dir: Path) -> int:
    """Rows still ``pending`` after refreshing statuses from the apply log."""
    rows = read_detail(run_dir)
    try:
        from enrichment.metrics import stamp_statuses_from_apply_log

        stamp_statuses_from_apply_log(run_dir, rows)
    except Exception:  # noqa: BLE001 — a bad apply log must not block the flush
        pass
    return sum(1 for r in rows if str(r.get("sf_update_status") or "").strip().lower() == "pending")


def needs_flush(run_dir: Path) -> int:
    """Pending row count when the batch never finished (no summary.json)."""
    if (run_dir / SUMMARY_JSON).is_file():
        return 0
    return pending_rows(run_dir)


def verdict_label(row: dict[str, str]) -> str:
    verdict = str(row.get("audit_verdict") or "").strip()
    if not verdict:
        return f"bucket {row.get('bucket') or '?'}"
    if verdict == "confirmed":
        kind = str(row.get("update_site_type") or "").strip()
        return "confirmed rooftop" if kind == "Rooftop" else f"confirmed {kind.lower() or 'other'}"
    return verdict


def aggregate_status(plan: Sequence[Sequence[Batch]]) -> dict[str, Any]:
    """Per-lane progress plus verdict / Salesforce / log totals."""
    verdicts: Counter = Counter()
    inconclusive: Counter = Counter()
    statuses: Counter = Counter()
    lanes_out = []
    rate_limits = tracebacks = 0
    for lane_batches in plan:
        done_sites = 0
        current = None
        finished = 0
        for b in lane_batches:
            if b.state == "running":
                current = b.chunk
            if b.state in {"done", "failed", "killed"}:
                finished += 1
            if b.state == "queued" or b.state == "skipped":
                continue
            rows = read_detail(b.run_dir)
            done_sites += len(rows)
            for row in rows:
                label = verdict_label(row)
                verdicts[label] += 1
                if label == "inconclusive":
                    inconclusive[row.get("audit_reason") or "?"] += 1
                statuses[row.get("sf_update_status") or "?"] += 1
            if b.log_path.is_file():
                text = b.log_path.read_text(encoding="utf-8", errors="replace")
                rate_limits += count_rate_limit_lines(text)
                tracebacks += len(TRACEBACK_RE.findall(text))
        lanes_out.append(
            {
                "lane": lane_batches[0].lane if lane_batches else len(lanes_out) + 1,
                "batch": current,
                "batches_finished": finished,
                "batches_total": len(lane_batches),
                "sites_done": done_sites,
                "failed": sum(1 for b in lane_batches if b.state == "failed"),
            }
        )
    return {
        "lanes": lanes_out,
        "sites_done": sum(lane["sites_done"] for lane in lanes_out),
        "verdicts": dict(verdicts.most_common()),
        "inconclusive_reasons": dict(inconclusive.most_common()),
        "sf_update_status": dict(statuses.most_common()),
        "rate_limit_warnings": rate_limits,
        "tracebacks": tracebacks,
    }


def batch_tag(chunk: int | None) -> str:
    return f"C{chunk}" if chunk else "-"


def format_status(status: dict[str, Any]) -> str:
    lines = [
        f"[{status.get('at', '')}] sites {status['sites_done']} | running "
        f"{status.get('running', 0)} | nearmap "
        + (f"{status['nearmap_mb']:.0f} MB" if status.get("nearmap_mb") is not None else "?")
        + f" | gemini 429/503 {status['rate_limit_warnings']}"
        f" | tracebacks {status['tracebacks']}"
        + (
            f" | stock {status['stock']} / owed {status['owed']}"
            if status.get("stock") is not None
            else ""
        )
        + (f" | STOP: {status['stop_reason']}" if status.get("stop_reason") else ""),
        f"  {'lane':>4} {'batch':>5} {'done':>6} {'sites':>6}",
    ]
    for lane in status["lanes"]:
        lines.append(
            f"  {lane['lane']:>4} {batch_tag(lane['batch']):>5} "
            f"{lane['batches_finished']:>3}/{lane['batches_total']:<2} {lane['sites_done']:>6}"
        )
    for label, n in status["verdicts"].items():
        lines.append(f"  {n:6}  {label}")
    if status["inconclusive_reasons"]:
        top = ", ".join(f"{k} {v}" for k, v in list(status["inconclusive_reasons"].items())[:4])
        lines.append(f"          inconclusive: {top}")
    lines.append(f"  salesforce: {status['sf_update_status']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ runner


def default_launcher(cmd, *, env, cwd, stdout, stderr):
    """Popen in its own process group so Ctrl+C reaches only the runner,
    which then decides whether to terminate and flush."""
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, env=env, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class LaneRunner:
    plan: list[list[Batch]]
    env_for: Callable[[Batch], dict[str, str]]
    flush_env_for: Callable[[Path], dict[str, str]] | None
    lanes_dir: Path
    launcher: Launcher = default_launcher
    cwd: Path = ROOT
    mb_fn: Callable[[], float] | None = None
    stop_at_mb: float | None = None
    coverage_fn: Callable[[], tuple[int, int]] | None = None
    out: Callable[[str], None] = print
    stop_reason: str = ""
    nearmap_mb: float | None = None
    stock: int | None = None
    owed: int | None = None
    flushed: list[dict[str, Any]] = field(default_factory=list)
    _stop_launch: threading.Event = field(default_factory=threading.Event)
    _killing: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _procs: dict[str, Any] = field(default_factory=dict)
    _threads: list[threading.Thread] = field(default_factory=list)

    @property
    def cmd(self) -> list[str]:
        return [sys.executable, "-m", "enrichment"]

    def event(self, msg: str) -> None:
        line = f"[{_now()}] {msg}"
        self.out(line)
        try:
            with (self.lanes_dir / EVENTS_LOG).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    # lanes
    def start(self) -> None:
        for lane_batches in self.plan:
            if not lane_batches:
                continue
            thread = threading.Thread(
                target=self._run_lane, args=(lane_batches,), daemon=True,
                name=f"lane-{lane_batches[0].lane}",
            )
            self._threads.append(thread)
            thread.start()

    def _run_lane(self, batches: list[Batch]) -> None:
        for b in batches:
            with self._lock:
                if self._stop_launch.is_set():
                    b.state = "skipped"
                    continue
                handle = None
                try:
                    b.log_path.parent.mkdir(parents=True, exist_ok=True)
                    handle = b.log_path.open("ab")
                    proc = self.launcher(
                        self.cmd, env=self.env_for(b), cwd=str(self.cwd),
                        stdout=handle, stderr=subprocess.STDOUT,
                    )
                except Exception as exc:  # noqa: BLE001 — one bad launch fails the batch only
                    if handle is not None:
                        handle.close()
                    b.state, b.ended = "failed", _now()
                    self.event(f"{b.name} launch failed: {exc}")
                    continue
                b.state, b.started = "running", _now()
                self._procs[b.name] = proc
            self.event(f"{b.name} start ({len(b.ids)} sites) -> {b.run_dir.name}")
            try:
                rc = proc.wait()
            finally:
                handle.close()
                with self._lock:
                    self._procs.pop(b.name, None)
            b.returncode, b.ended = rc, _now()
            b.state = "killed" if self._killing.is_set() else ("done" if rc == 0 else "failed")
            self.event(f"{b.name} {b.state} (exit {rc})")

    def alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def running(self) -> int:
        with self._lock:
            return len(self._procs)

    def join(self, timeout: float | None = None) -> None:
        for t in self._threads:
            t.join(timeout)

    # stops
    def stop_launching(self, reason: str) -> None:
        with self._lock:
            if not self._stop_launch.is_set():
                self._stop_launch.set()
                self.stop_reason = reason
                self.event(f"stop launching new batches: {reason}")

    def terminate(self, reason: str, grace_s: float = 15.0) -> None:
        self.stop_launching(reason)
        self._killing.set()
        with self._lock:
            procs = list(self._procs.items())
        for name, proc in procs:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001 — already gone
                pass
        deadline = time.monotonic() + grace_s
        for name, proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if procs:
            self.event(f"terminated {len(procs)} running batch(es): {[n for n, _ in procs]}")

    def check(self) -> None:
        """Budget / coverage checks (call every few seconds)."""
        if self.mb_fn is not None:
            try:
                self.nearmap_mb = float(self.mb_fn())
            except Exception as exc:  # noqa: BLE001 — keep running, report it
                self.event(f"nearmap check failed: {exc}")
        if (
            self.stop_at_mb is not None
            and self.nearmap_mb is not None
            and self.nearmap_mb >= self.stop_at_mb
            and not self._stop_launch.is_set()
        ):
            self.stop_launching(f"nearmap {self.nearmap_mb:.0f} MB >= {self.stop_at_mb:g}")
        if self.coverage_fn is not None and not self._killing.is_set():
            try:
                self.stock, self.owed = self.coverage_fn()
            except Exception as exc:  # noqa: BLE001
                self.event(f"coverage check failed: {exc}")
                return
            if self.owed and self.stock >= self.owed:
                self.terminate(f"covered: stock {self.stock} >= owed {self.owed}")

    # flush
    def flush_candidates(self) -> list[tuple[Batch, int]]:
        out = []
        for lane_batches in self.plan:
            for b in lane_batches:
                if b.state in {"queued", "skipped"}:
                    continue
                n = needs_flush(b.run_dir)
                if n:
                    out.append((b, n))
        return out

    def flush(self) -> list[dict[str, Any]]:
        """Push finished-but-unsent rows of interrupted batches. A Ctrl+C here
        terminates the current push and skips the rest."""
        if self.flush_env_for is None:
            return []
        todo = self.flush_candidates()
        if not todo:
            return []
        self.event(f"flushing {len(todo)} interrupted batch(es) with APPLY_EXISTING=1")
        for i, (b, pending) in enumerate(todo):
            log_path = self.lanes_dir / f"flush_{b.name}.log"
            proc = None
            try:
                with log_path.open("ab") as handle:
                    proc = self.launcher(
                        self.cmd, env=self.flush_env_for(b.run_dir), cwd=str(self.cwd),
                        stdout=handle, stderr=subprocess.STDOUT,
                    )
                    rc = proc.wait()
            except KeyboardInterrupt:
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                left = [x.run_dir for x, _ in todo[i:]]
                self.event("flush aborted; finish by hand with CONNECTX_AUDIT=1 APPLY=1 "
                           "APPLY_EXISTING=1 RUN_DIR=<dir> python -m enrichment for:")
                for run_dir in left:
                    self.event(f"  {run_dir}")
                self.flushed.append({"run_dir": str(b.run_dir), "pending": pending, "exit": "aborted"})
                break
            self.flushed.append({"run_dir": str(b.run_dir), "pending": pending, "exit": rc})
            self.event(f"flushed {b.run_dir.name}: {pending} pending row(s), exit {rc}")
        return self.flushed

    # status
    def status(self) -> dict[str, Any]:
        status = aggregate_status(self.plan)
        status.update(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "running": self.running(),
                "nearmap_mb": self.nearmap_mb,
                "stop_at_mb": self.stop_at_mb,
                "stock": self.stock,
                "owed": self.owed,
                "stop_reason": self.stop_reason,
                "flushed": self.flushed,
                "batches": [
                    {"name": b.name, "state": b.state, "exit": b.returncode,
                     "started": b.started, "ended": b.ended, "run_dir": b.run_dir.name}
                    for lane_batches in self.plan for b in lane_batches
                ],
            }
        )
        return status

    def write_status(self, *, echo: bool = True) -> dict[str, Any]:
        status = self.status()
        try:
            (self.lanes_dir / STATUS_JSON).write_text(json.dumps(status, indent=2), encoding="utf-8")
        except OSError:
            pass
        if echo:
            self.out(format_status(status))
        return status

    def run(self, *, check_every_s: float = 30.0, status_every_s: float = 120.0,
            poll_s: float = 1.0) -> int:
        """Start lanes, watch until done or stopped, flush. Returns exit code."""
        interrupted = skip_flush = False
        self.check()
        self.start()
        next_check = time.monotonic() + check_every_s
        next_status = time.monotonic() + status_every_s
        try:
            while self.alive():
                time.sleep(poll_s)
                now = time.monotonic()
                if now >= next_check:
                    self.check()
                    next_check = now + check_every_s
                if now >= next_status:
                    self.write_status()
                    next_status = now + status_every_s
        except KeyboardInterrupt:
            interrupted = True
            self.out("Ctrl+C: stopping lanes (Ctrl+C again aborts the flush)")
            try:
                self.terminate("Ctrl+C")
                self.join(30)
            except KeyboardInterrupt:
                skip_flush = True
        if not skip_flush:
            try:
                self.flush()
            except KeyboardInterrupt:
                interrupted = True
        final = self.write_status()
        if interrupted:
            return 130
        failed = any(b["state"] == "failed" for b in final["batches"])
        return 1 if failed else 0


# --------------------------------------------------------------------- CLI


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m enrichment.lanes",
        description="Run python -m enrichment as N parallel lanes of fixed-size batches.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--ids-file", type=Path, help="one Salesforce Id per line")
    src.add_argument("--pool-audit", action="store_true",
                     help="queue = pool ConnectX Rooftops in New/Unreviewed minus prior audits")
    p.add_argument("--priority-csv", type=Path, help="Id,tier CSV; lower tier runs first")
    p.add_argument("--lanes", type=int, default=5)
    p.add_argument("--batch", type=int, default=75, help="sites per python -m enrichment")
    p.add_argument("--workers", type=int, default=4, help="CLASSIFY_WORKERS per lane")
    p.add_argument("--gemini-rpm-total", type=float, default=None, help="default env GEMINI_RPM or 60")
    p.add_argument("--claude-rpm-total", type=float, default=None, help="default env CLAUDE_RPM or 50")
    p.add_argument("--stop-at-mb", type=float, default=None,
                   help="stop launching batches once month-to-date Nearmap MB >= this")
    p.add_argument("--stop-when-covered", action="store_true",
                   help="terminate + flush once confirmed pool rooftops >= open owed rep slots")
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                   help="passed to every batch (repeatable)")
    p.add_argument("--status-every", type=float, default=120.0, help="seconds between status tables")
    p.add_argument("--check-every", type=float, default=30.0, help="seconds between budget/coverage checks")
    p.add_argument("--run-name", default=None, help="default: pool (with --pool-audit) or lanes")
    p.add_argument("--dry-run", action="store_true", help="print the plan; launch nothing")
    return p


def _rpm_total(arg: float | None, env_name: str, default: float) -> float:
    if arg is not None:
        return arg
    from envutil import env_float

    return env_float(env_name, default)


def main(argv: Sequence[str] | None = None, *, launcher: Launcher = default_launcher) -> int:
    parent_env = dict(os.environ)  # before .env: the child loads .env itself
    args = _parser().parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from paths import runs_dir

    try:
        extra = lane_extra_env(parse_env_pairs(args.env), pool_audit=args.pool_audit)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.lanes < 1 or args.batch < 1 or args.workers < 1:
        print("error: --lanes, --batch and --workers must be >= 1", file=sys.stderr)
        return 2

    if args.pool_audit:
        from salesforce.sf_client import SalesforceClient

        print("building pool queue (read-only Salesforce query) ...", flush=True)
        ids, found, dropped = build_pool_queue(SalesforceClient(), runs_dir())
        print(f"  pool New/Unreviewed ConnectX rooftops: {found}; already audited: {dropped}; "
              f"queue: {len(ids)}", flush=True)
    else:
        ids = read_ids_file(args.ids_file)
        print(f"  ids file: {len(ids)} Id(s)", flush=True)
    if args.priority_csv:
        ids = order_queue(ids, read_priority_csv(args.priority_csv))
    if not ids:
        print("queue is empty; nothing to do", flush=True)
        return 0

    lanes = min(args.lanes, max(1, -(-len(ids) // args.batch)))
    gemini = split_rpm(_rpm_total(args.gemini_rpm_total, "GEMINI_RPM", 60), lanes)
    claude = split_rpm(_rpm_total(args.claude_rpm_total, "CLAUDE_RPM", 50), lanes)
    name = args.run_name or ("pool" if args.pool_audit else "lanes")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    runs_root = runs_dir()
    lanes_dir = runs_root / f"{stamp}_lanes_{name}"
    suffix = run_suffix({**extra})
    plan = build_plan(ids, lanes=lanes, batch=args.batch, runs_root=runs_root,
                      lanes_dir=lanes_dir, stamp=stamp, name=name, suffix=suffix)

    def env_for(b: Batch) -> dict[str, str]:
        return build_batch_env(parent_env, ids=b.ids, run_dir=b.run_dir, batch_size=args.batch,
                               workers=args.workers, gemini_rpm=gemini, claude_rpm=claude,
                               extra=extra)

    sample = env_for(plan[0][0])
    shown = {k: sample[k] for k in sorted(sample)
             if k in extra or k in {"APPLY", "LIMIT", "CLASSIFY_WORKERS", "GEMINI_RPM",
                                    "CLAUDE_RPM", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
                                    "NEARMAP_MONTHLY_BUDGET_MB"}}
    plan_doc = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "argv": list(argv) if argv is not None else sys.argv[1:],
        "queue": len(ids),
        "lanes": lanes,
        "batch": args.batch,
        "gemini_rpm_per_lane": gemini,
        "claude_rpm_per_lane": claude,
        "stop_at_mb": args.stop_at_mb,
        "stop_when_covered": args.stop_when_covered,
        "env": shown,
        "plan": [
            [{"chunk": b.chunk, "sites": len(b.ids), "run_dir": str(b.run_dir),
              "log": str(b.log_path), "first_id": b.ids[0]} for b in lane_batches]
            for lane_batches in plan
        ],
    }
    print(f"{len(ids)} sites -> {sum(map(len, plan))} batch(es) of <= {args.batch} over "
          f"{lanes} lane(s); per lane GEMINI_RPM={gemini} CLAUDE_RPM={claude} "
          f"CLASSIFY_WORKERS={args.workers}", flush=True)
    print("  env: " + " ".join(f"{k}={v}" for k, v in shown.items()), flush=True)
    for lane_batches in plan:
        chunks = ", ".join(f"C{b.chunk}({len(b.ids)})" for b in lane_batches)
        print(f"  lane {lane_batches[0].lane}: {chunks}", flush=True)
    print(f"  runs: {runs_root / (stamp + '_' + name + '_L<lane>_C<chunk>' + suffix)}", flush=True)
    if args.stop_at_mb is not None:
        try:
            now_mb = f"{month_to_date_mb():.0f}"
        except Exception:  # noqa: BLE001 — informational only
            now_mb = "?"
        print(f"  stop at {args.stop_at_mb:g} MB month-to-date Nearmap (now {now_mb} MB)", flush=True)
    if args.stop_when_covered:
        print("  stop + flush when confirmed pool rooftops >= open owed rep slots", flush=True)
    if not _truthy(extra.get("APPLY", "1")):
        print("  APPLY=0: dry-run batches, no flush", flush=True)
    if args.dry_run:
        print("dry run: nothing launched", flush=True)
        return 0

    lanes_dir.mkdir(parents=True, exist_ok=True)
    (lanes_dir / PLAN_JSON).write_text(json.dumps(plan_doc, indent=2), encoding="utf-8")
    print(f"  lanes folder: {lanes_dir}", flush=True)
    apply_on = _truthy(extra.get("APPLY", "1"))
    runner = LaneRunner(
        plan=plan,
        env_for=env_for,
        flush_env_for=(lambda run_dir: flush_env(parent_env, run_dir, extra)) if apply_on else None,
        lanes_dir=lanes_dir,
        launcher=launcher,
        mb_fn=month_to_date_mb,
        stop_at_mb=args.stop_at_mb,
        coverage_fn=(lambda: coverage_counts(runs_root)) if args.stop_when_covered else None,
    )
    return runner.run(check_every_s=args.check_every, status_every_s=args.status_every)


if __name__ == "__main__":
    raise SystemExit(main())
