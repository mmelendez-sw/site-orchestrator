"""Terminal stage banners for enrichment runs."""

from __future__ import annotations

import sys
import time
from typing import Any

_run_t0: float | None = None
_stage_t0: float | None = None
_stage_title: str = ""


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
    print(line, flush=True)
    if detail:
        print(f"    {detail}", flush=True)


def step(message: str) -> None:
    print(f"  → {message}", flush=True)


def result(message: str, *, elapsed_s: float | None = None) -> None:
    """Print a success line; elapsed_s overrides stage elapsed when provided."""
    seconds = stage_elapsed() if elapsed_s is None else elapsed_s
    print(f"  ✓ {message}  ({format_duration(seconds)})", flush=True)


def warn(message: str, *, elapsed_s: float | None = None) -> None:
    seconds = stage_elapsed() if elapsed_s is None else elapsed_s
    print(
        f"  ! {message}  ({format_duration(seconds)})",
        flush=True,
        file=sys.stderr,
    )


def dump_summary(summary: dict[str, Any]) -> None:
    stage("COMPLETE — RUN SUMMARY")
    for key, value in summary.items():
        print(f"    {key}: {value}", flush=True)
    print(f"    elapsed: {format_duration(run_elapsed())}", flush=True)
