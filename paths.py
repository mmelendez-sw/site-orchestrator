"""Local CSVs, run folders, and chips live outside this git repo.

Default: ../site-orchestrator-data (sibling of site-orchestrator).
Override with SITE_ORCHESTRATOR_DATA.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def data_root() -> Path:
    override = (os.environ.get("SITE_ORCHESTRATOR_DATA") or "").strip()
    if override:
        return Path(override)
    return REPO_ROOT.parent / "site-orchestrator-data"


def inputs_dir() -> Path:
    return data_root() / "inputs"


def datasets_dir() -> Path:
    return data_root() / "data"


def runs_dir() -> Path:
    return data_root() / "runs"


def chips_dir() -> Path:
    return data_root() / "chips"


def metrics_dir() -> Path:
    return data_root() / "metrics"


def ensure_data_layout() -> Path:
    """Create the sibling data tree if missing. Returns the root."""
    root = data_root()
    for path in (
        root,
        inputs_dir(),
        datasets_dir(),
        runs_dir(),
        chips_dir(),
        metrics_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
    return root
