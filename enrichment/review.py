"""Build a human review package for enrichment candidates before SF apply."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from enrichment.constants import (
    CANDIDATE_CSV,
    DETAIL_CSV,
    REVIEW_DIR_NAME,
    REVIEW_INDEX_HTML,
    REVIEW_MANIFEST_CSV,
)

REVIEW_MANIFEST_COLUMNS: tuple[str, ...] = (
    "approved",
    "Id",
    "update_site_type",
    "update_lat",
    "update_lng",
    "update_coord_source",
    "update_verified_site_source",
    "naip_site_type",
    "naip_site_confidence",
    "naip_cell_equipment",
    "cell_equipment_confidence",
    "cell_gear_kind",
    "cell_equipment_evidence",
    "site_evidence",
    "nearmap_tier",
    "imagery_used",
    "primary_model",
    "escalation_model",
    "cell_models_agree",
    "asset_offset_m",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "chip_links",
)


def write_review_package(
    run_dir: Path,
    *,
    candidates: Sequence[dict[str, Any]] | None = None,
) -> Path:
    """Write run_dir/review/ with manifest CSV + index.html for visual QA.

    Returns the review directory path.
    """
    run_dir = Path(run_dir)
    review_dir = run_dir / REVIEW_DIR_NAME
    review_dir.mkdir(parents=True, exist_ok=True)

    if candidates is None:
        candidates = _load_candidates(run_dir)

    chip_dir = run_dir / "chips"
    rows: list[dict[str, Any]] = []
    for row in candidates:
        sid = str(row.get("Id") or "").strip()
        chips = _chip_names_for_id(chip_dir, sid)
        entry = {col: row.get(col, "") for col in REVIEW_MANIFEST_COLUMNS if col != "approved"}
        entry["approved"] = ""  # blank until human marks yes
        entry["chip_links"] = ";".join(chips)
        # Ensure keys exist even if missing from candidate csv
        for col in REVIEW_MANIFEST_COLUMNS:
            entry.setdefault(col, "")
        rows.append(entry)

    manifest_path = review_dir / REVIEW_MANIFEST_CSV
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_MANIFEST_COLUMNS)
        writer.writeheader()
        for entry in rows:
            writer.writerow({k: entry.get(k, "") for k in REVIEW_MANIFEST_COLUMNS})

    index_path = review_dir / REVIEW_INDEX_HTML
    index_path.write_text(
        _render_index_html(run_dir=run_dir, rows=rows),
        encoding="utf-8",
    )

    readme = review_dir / "README.txt"
    readme.write_text(
        "\n".join(
            [
                "Enrichment review package",
                "=========================",
                "1. Open index.html in a browser.",
                "2. Check Approve on sites that truly show cellular gear.",
                "3. Click 'Download review_manifest.csv' and replace this folder's",
                "   review_manifest.csv with the downloaded file.",
                "4. Push approved rows only:",
                "   python -m enrichment --apply-reviewed --run-dir <this-run>",
                "",
                "Or approve every candidate without editing:",
                "   python -m enrichment --apply-reviewed --approve-all --run-dir <this-run>",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return review_dir


def load_approved_ids(review_dir: Path, *, approve_all: bool = False) -> set[str]:
    """Return Salesforce Ids marked approved=yes in the review manifest."""
    manifest = Path(review_dir) / REVIEW_MANIFEST_CSV
    if not manifest.exists():
        raise FileNotFoundError(f"Review manifest not found: {manifest}")
    approved: set[str] = set()
    with manifest.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            sid = str(row.get("Id") or "").strip()
            if not sid:
                continue
            if approve_all:
                approved.add(sid)
                continue
            flag = str(row.get("approved") or "").strip().lower()
            if flag in {"yes", "y", "true", "1", "approve", "approved"}:
                approved.add(sid)
    return approved


def _load_candidates(run_dir: Path) -> list[dict[str, Any]]:
    candidate_csv = run_dir / CANDIDATE_CSV
    detail_csv = run_dir / DETAIL_CSV
    if candidate_csv.exists():
        with candidate_csv.open(encoding="utf-8-sig", newline="") as handle:
            candidates = list(csv.DictReader(handle))
    else:
        candidates = []

    detail_by_id: dict[str, dict[str, Any]] = {}
    if detail_csv.exists():
        with detail_csv.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                detail_by_id[str(row.get("Id") or "")] = row

    merged: list[dict[str, Any]] = []
    for row in candidates:
        sid = str(row.get("Id") or "")
        detail = detail_by_id.get(sid) or {}
        merged.append({**detail, **row})
    return merged


def _chip_names_for_id(chip_dir: Path, site_id: str) -> list[str]:
    if not site_id or not chip_dir.is_dir():
        return []
    names = sorted(p.name for p in chip_dir.glob(f"{site_id}_*") if p.is_file())
    # Prefer Nearmap/oblique first for review readability.
    def rank(name: str) -> tuple[int, str]:
        lower = name.lower()
        if "nearmap_north" in lower or "nearmap_east" in lower:
            return (0, name)
        if "nearmap_vert" in lower:
            return (1, name)
        if "nearmap_" in lower:
            return (2, name)
        if "naip" in lower:
            return (3, name)
        return (4, name)

    return sorted(names, key=rank)


def _render_index_html(*, run_dir: Path, rows: Sequence[dict[str, Any]]) -> str:
    cards = []
    for idx, row in enumerate(rows):
        sid = html.escape(str(row.get("Id") or ""))
        street = html.escape(
            ", ".join(
                p
                for p in (
                    str(row.get("Site_Street__c") or "").strip(),
                    str(row.get("Site_City__c") or "").strip(),
                    str(row.get("Site_State__c") or "").strip(),
                )
                if p
            )
        )
        chips = [c for c in str(row.get("chip_links") or "").split(";") if c]
        imgs = []
        for name in chips[:6]:
            src = html.escape(f"../chips/{name}")
            imgs.append(
                f'<a href="{src}" target="_blank" rel="noopener">'
                f'<img src="{src}" alt="{html.escape(name)}" loading="lazy"/>'
                f"</a>"
            )
        evidence = html.escape(str(row.get("cell_equipment_evidence") or "")[:400])
        site_ev = html.escape(str(row.get("site_evidence") or "")[:300])
        meta = (
            f"type={html.escape(str(row.get('update_site_type') or ''))} · "
            f"tier={html.escape(str(row.get('nearmap_tier') or ''))} · "
            f"cell_conf={html.escape(str(row.get('cell_equipment_confidence') or ''))} · "
            f"gear={html.escape(str(row.get('cell_gear_kind') or ''))} · "
            f"agree={html.escape(str(row.get('cell_models_agree') or ''))} · "
            f"offset={html.escape(str(row.get('asset_offset_m') or ''))}m"
        )
        cards.append(
            f"""
<article class="card" data-id="{sid}">
  <header>
    <label class="approve">
      <input type="checkbox" class="approve-box" data-id="{sid}"/>
      Approve
    </label>
    <div>
      <h2>{sid}</h2>
      <p class="addr">{street or "—"}</p>
      <p class="meta">{meta}</p>
    </div>
  </header>
  <p class="evidence"><strong>Cell evidence:</strong> {evidence or "—"}</p>
  <p class="evidence"><strong>Site evidence:</strong> {site_ev or "—"}</p>
  <div class="chips">{"".join(imgs) or "<p>No chips</p>"}</div>
</article>
"""
        )

    payload = json.dumps(
        [{k: row.get(k, "") for k in REVIEW_MANIFEST_COLUMNS} for row in rows]
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Enrichment review — {html.escape(run_dir.name)}</title>
  <style>
    :root {{ color-scheme: light; font-family: Segoe UI, system-ui, sans-serif; }}
    body {{ margin: 0; background: #f4f4f1; color: #1a1a1a; }}
    header.top {{ position: sticky; top: 0; background: #111; color: #fff;
      padding: 12px 20px; display: flex; gap: 16px; align-items: center;
      justify-content: space-between; z-index: 5; }}
    header.top button {{ cursor: pointer; padding: 8px 14px; border: 0;
      background: #e8e8e3; color: #111; font-weight: 600; }}
    main {{ padding: 20px; max-width: 1200px; margin: 0 auto; display: grid; gap: 20px; }}
    .card {{ background: #fff; border: 1px solid #d8d8d2; padding: 16px; }}
    .card header {{ display: flex; gap: 16px; align-items: flex-start; }}
    .approve {{ display: flex; gap: 8px; align-items: center; font-weight: 700;
      min-width: 110px; }}
    h2 {{ margin: 0 0 4px; font-size: 16px; }}
    .addr, .meta, .evidence {{ margin: 4px 0; font-size: 13px; color: #333; }}
    .chips {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 8px; margin-top: 12px; }}
    .chips img {{ width: 100%; height: 140px; object-fit: cover; background: #eee; }}
    .hint {{ font-size: 13px; opacity: 0.85; }}
  </style>
</head>
<body>
  <header class="top">
    <div>
      <div><strong>Review candidates</strong> — {html.escape(run_dir.name)}</div>
      <div class="hint">Check Approve on true cell sites, then download the manifest and replace review_manifest.csv</div>
    </div>
    <div>
      <span id="count">0 approved</span>
      &nbsp;
      <button type="button" id="download">Download review_manifest.csv</button>
    </div>
  </header>
  <main>
    {"".join(cards) if cards else "<p>No potential_update candidates in this run.</p>"}
  </main>
  <script>
    const rows = {payload};
    const boxes = [...document.querySelectorAll('.approve-box')];
    const countEl = document.getElementById('count');
    function refresh() {{
      const n = boxes.filter(b => b.checked).length;
      countEl.textContent = n + ' approved';
    }}
    boxes.forEach(b => b.addEventListener('change', refresh));
    refresh();
    document.getElementById('download').addEventListener('click', () => {{
      const approved = new Set(boxes.filter(b => b.checked).map(b => b.dataset.id));
      const cols = {json.dumps(list(REVIEW_MANIFEST_COLUMNS))};
      const lines = [cols.join(',')];
      for (const row of rows) {{
        const out = {{...row, approved: approved.has(row.Id) ? 'yes' : ''}};
        lines.push(cols.map(c => csvEscape(out[c] ?? '')).join(','));
      }}
      const blob = new Blob([lines.join('\\n')], {{type: 'text/csv;charset=utf-8'}});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'review_manifest.csv';
      a.click();
      URL.revokeObjectURL(a.href);
    }});
    function csvEscape(v) {{
      const s = String(v).replace(/\\r?\\n/g, ' ');
      if (/[",]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
      return s;
    }}
  </script>
</body>
</html>
"""
