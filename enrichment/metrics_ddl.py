"""Load enrichment metrics DDL from sql/enrichment_metrics.sql (GO batches)."""

from __future__ import annotations

import re
from pathlib import Path

SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "enrichment_metrics.sql"

_GO = re.compile(r"(?im)^\s*GO\s*;?\s*$")


def ddl_statements(sql_text: str | None = None) -> tuple[str, ...]:
    """Split SSMS batches. CREATE VIEW must be its own batch (after GO)."""
    text = sql_text if sql_text is not None else SQL_FILE.read_text(encoding="utf-8")
    batches: list[str] = []
    for part in _GO.split(text):
        lines = [
            line
            for line in part.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        stmt = "\n".join(lines).strip()
        if stmt:
            batches.append(stmt)
    return tuple(batches)
