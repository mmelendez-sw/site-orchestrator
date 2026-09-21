"""Terminal stage banners for enrichment runs."""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_run_t0: float | None = None
_stage_t0: float | None = None
_stage_title: str = ""
_SPINNER_FRAMES = "|/-\\"
_SPINNER_INTERVAL_S = 0.25


def _stdout_is_tty() -> bool:
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty and isatty())


def _safe_print(*args: Any, **kwargs: Any) -> None:
    """Print without crashing on cp1252 consoles (checkmark, arrows, dashes)."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(kwargs.get("file") or sys.stdout, "encoding", None) or "ascii"
        safe_args = [
            str(arg).encode(encoding, errors="replace").decode(encoding, errors="replace")
            for arg in args
        ]
        print(*safe_args, **kwargs)


def _safe_text(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def format_busy_line(label: str, *, frame: str = "", elapsed_s: float | None = None) -> str:
    """Compact live-status line used by ``busy()`` and quiet row counts."""
    seconds = run_elapsed() if elapsed_s is None else elapsed_s
    suffix = f"  {frame}" if frame else ""
    return f"{label} | run {format_duration(seconds)}{suffix}"


def _write_live(text: str, *, width: int) -> int:
    """Rewrite the current stdout line; return the visible width for padding."""
    text = _safe_text(text)
    pad = max(0, width - len(text))
    sys.stdout.write("\r" + text + (" " * pad))
    sys.stdout.flush()
    return max(width, len(text))


@contextmanager
def busy(label: str) -> Iterator[None]:
    """Show that a quiet run is still alive.

    On a TTY, rewrite one line with a spinner and run elapsed until the
    block returns. Off a TTY (tests, redirected logs), print the label once.
    """
    prefix = (label or "working").strip() or "working"
    if not _stdout_is_tty():
        _safe_print(format_busy_line(prefix), flush=True)
        yield
        return

    stop = threading.Event()
    width = 0

    def render(frame: str) -> None:
        nonlocal width
        width = _write_live(format_busy_line(prefix, frame=frame), width=width)

    def spin() -> None:
        index = 0
        render(_SPINNER_FRAMES[0])
        while not stop.wait(_SPINNER_INTERVAL_S):
            index += 1
            render(_SPINNER_FRAMES[index % len(_SPINNER_FRAMES)])

    thread = threading.Thread(target=spin, daemon=True, name="enrichment-busy")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
        render("")
        sys.stdout.write("\n")
        sys.stdout.flush()


def format_duration(seconds: float) -> str:
    """Human-readable duration for terminal output."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def reset_run_timer() -> None:
    """Start (or restart) the whole-run clock."""
    global _run_t0, _stage_t0, _stage_title
    _run_t0 = time.monotonic()
    _stage_t0 = _run_t0
    _stage_title = ""


def run_elapsed() -> float:
    if _run_t0 is None:
        return 0.0
    return time.monotonic() - _run_t0


def stage_elapsed() -> float:
    if _stage_t0 is None:
        return 0.0
    return time.monotonic() - _stage_t0


def stage(title: str, detail: str | None = None) -> None:
    """Print a high-visibility stage line to stdout."""
    global _run_t0, _stage_t0, _stage_title
    now = time.monotonic()
    if _run_t0 is None:
        _run_t0 = now
    _stage_t0 = now
    _stage_title = title

    line = f"\n=== STAGE: {title} ===  [run {format_duration(now - _run_t0)}]"
    _safe_print(line, flush=True)
    if detail:
        _safe_print(f"    {detail}", flush=True)


def step(message: str) -> None:
    _safe_print(f"  -> {message}", flush=True)


def row_count(
    index: int,
    total: int,
    *,
    sf_id: str = "",
    address: str = "",
) -> None:
    """Compact per-row progress: count, Salesforce Id, address, run elapsed."""
    sid = (sf_id or "").strip() or "-"
    addr = (address or "").strip() or "-"
    _safe_print(
        f"[{index}/{total}] {sid} | {addr} | run {format_duration(run_elapsed())}",
        flush=True,
    )


def format_site_address(row: dict[str, Any]) -> str:
    """Street, city, state from an SF/enrichment row."""
    parts = [
        str(row.get("Site_Street__c") or "").strip(),
        str(row.get("Site_City__c") or "").strip(),
        str(row.get("Site_State__c") or "").strip(),
    ]
    return ", ".join(p for p in parts if p)


def result(message: str, *, elapsed_s: float | None = None) -> None:
    """Print a success line; elapsed_s overrides stage elapsed when provided."""
    seconds = stage_elapsed() if elapsed_s is None else elapsed_s
    _safe_print(f"  + {message}  ({format_duration(seconds)})", flush=True)


def warn(message: str, *, elapsed_s: float | None = None) -> None:
    seconds = stage_elapsed() if elapsed_s is None else elapsed_s
    _safe_print(
        f"  ! {message}  ({format_duration(seconds)})",
        flush=True,
        file=sys.stderr,
    )


def dump_summary(summary: dict[str, Any]) -> None:
    """Print this-run metrics and cumulative KPIs (same fields as the ledger)."""
    from enrichment.metrics import KPI_METRIC_KEYS, RUN_METRIC_KEYS, metric_lines

    run = summary.get("run") if isinstance(summary.get("run"), dict) else summary
    run_id = (run or {}).get("run_id") or Path(str(summary.get("run_dir") or "")).name
    stage("COMPLETE — THIS RUN", str(run_id or summary.get("run_dir") or ""))
    for line in metric_lines(run if isinstance(run, dict) else {}, RUN_METRIC_KEYS):
        _safe_print(line, flush=True)
    kpis = summary.get("kpis") if isinstance(summary.get("kpis"), dict) else None
    if kpis:
        stage("CUMULATIVE KPIs", "last Salesforce Id wins")
        for line in metric_lines(kpis, KPI_METRIC_KEYS):
            _safe_print(line, flush=True)
    _safe_print(f"    elapsed: {format_duration(run_elapsed())}", flush=True)
