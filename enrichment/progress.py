"""Terminal stage banners for enrichment runs."""

from __future__ import annotations

import sys
from typing import Any


def stage(title: str, detail: str | None = None) -> None:
    """Print a high-visibility stage line to stdout."""
    line = f"\n=== STAGE: {title} ==="
    print(line, flush=True)
    if detail:
        print(f"    {detail}", flush=True)


def step(message: str) -> None:
    print(f"  → {message}", flush=True)


def result(message: str) -> None:
    print(f"  ✓ {message}", flush=True)


def warn(message: str) -> None:
    print(f"  ! {message}", flush=True, file=sys.stderr)


def dump_summary(summary: dict[str, Any]) -> None:
    stage("COMPLETE — RUN SUMMARY")
    for key, value in summary.items():
        print(f"    {key}: {value}", flush=True)
