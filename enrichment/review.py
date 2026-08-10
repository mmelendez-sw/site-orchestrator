"""Build a human review package for enrichment candidates before SF apply."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any, Sequence

from enrichment.constants import (
    CANDIDATE_CSV,
    DETAIL_CSV,
    REVIEW_DIR_NAME,
    REVIEW_INDEX_HTML,
    REVIEW_MANIFEST_CSV,
)

REVIEW_MANIFEST_COLUMNS: tuple[str, ...] = (
    "approved",
    "review_section",
    "bucket",
    "holdout_reason",
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
    "gemini_cell_equipment",
    "claude_cell_equipment",
    "cell_models_agree",
    "asset_offset_m",
    "asset_box_2d",
    "asset_view",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "chip_links",
)


def write_review_package(
    run_dir: Path,
    *,
    candidates: Sequence[dict[str, Any]] | None = None,
    rooftop_holdouts: Sequence[dict[str, Any]] | None = None,
) -> Path:
    """Write run_dir/review/ with manifest CSV + index.html for visual QA.

    Includes potential_update candidates and potential_rooftop holdouts so you
    can inspect Claude/Gemini disagreements before deciding what to push.
    """
    run_dir = Path(run_dir)
    review_dir = run_dir / REVIEW_DIR_NAME
    review_dir.mkdir(parents=True, exist_ok=True)

    if candidates is None and rooftop_holdouts is None:
        candidates, rooftop_holdouts = _load_review_rows(run_dir)
    else:
        candidates = list(candidates or [])
        rooftop_holdouts = list(rooftop_holdouts or [])

    chip_dir = run_dir / "chips"
    rows: list[dict[str, Any]] = []
    for section, group in (
        ("candidate", candidates),
        ("rooftop_holdout", rooftop_holdouts),
    ):
        for row in group:
            sid = str(row.get("Id") or "").strip()
            chips = _chip_names_for_id(chip_dir, sid)
            entry = {
                col: row.get(col, "")
                for col in REVIEW_MANIFEST_COLUMNS
                if col not in {"approved", "review_section", "chip_links"}
            }
            entry["approved"] = ""
            entry["review_section"] = section
            entry["bucket"] = row.get("bucket") or (
                "potential_update" if section == "candidate" else "potential_rooftop"
            )
            entry["holdout_reason"] = row.get("holdout_reason") or ""
            entry["chip_links"] = ";".join(chips)
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
                "2. SF candidates: check Approve on true cellular sites to push.",
                "3. Rooftop holdouts: visual QA only by default (model said rooftop",
                "   but cell gear did not clear dual-model / confidence gates).",
                "4. Lime outline on a chip = model asset_box_2d on that view.",
                "5. Download review_manifest.csv and replace this folder's copy.",
                "6. Push approved SF candidates:",
                "   python -m enrichment --apply-reviewed --run-dir <this-run>",
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


def _load_review_rows(
    run_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    detail_csv = run_dir / DETAIL_CSV
    detail_by_id: dict[str, dict[str, Any]] = {}
    if detail_csv.exists():
        with detail_csv.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                detail_by_id[str(row.get("Id") or "")] = row

    candidate_csv = run_dir / CANDIDATE_CSV
    candidates: list[dict[str, Any]] = []
    if candidate_csv.exists():
        with candidate_csv.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sid = str(row.get("Id") or "")
                candidates.append({**(detail_by_id.get(sid) or {}), **row})
    else:
        candidates = [
            r
            for r in detail_by_id.values()
            if r.get("bucket") == "potential_update"
        ]

    holdouts = [
        r
        for r in detail_by_id.values()
        if r.get("bucket") == "potential_rooftop"
    ]
    return candidates, holdouts


def _load_candidates(run_dir: Path) -> list[dict[str, Any]]:
    candidates, _holdouts = _load_review_rows(run_dir)
    return candidates


def parse_asset_box(raw: Any) -> list[int] | None:
    """Parse Gemini-style [ymin, xmin, ymax, xmax] in 0–1000 coords."""
    value: Any = raw
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        ymin, xmin, ymax, xmax = (int(v) for v in value[:4])
    except (TypeError, ValueError):
        return None
    if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
        return None
    return [ymin, xmin, ymax, xmax]


def chip_matches_asset_view(chip_name: str, asset_view: str | None) -> bool:
    """True when chip_name is the view the model drew asset_box_2d on."""
    view = str(asset_view or "").strip().lower()
    if not view:
        return False
    name = chip_name.lower()
    if "north" in view and "nearmap_north" in name:
        return True
    if "east" in view and "nearmap_east" in name:
        return True
    if "south" in view and "nearmap_south" in name:
        return True
    if "west" in view and "nearmap_west" in name:
        return True
    if ("top-down" in view or "vert" in view) and "nearmap_vert" in name:
        return True
    if "naip" in view and "wide" in view and "naip_wide" in name:
        return True
    if "naip" in view and "naip" in name and "wide" not in name and "nearmap" not in name:
        return True
    return False


def _chip_names_for_id(chip_dir: Path, site_id: str) -> list[str]:
    if not site_id or not chip_dir.is_dir():
        return []
    names = sorted(p.name for p in chip_dir.glob(f"{site_id}_*") if p.is_file())

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


def chip_display_title(chip_name: str) -> str:
    """Human label for a saved chip filename (for review UI)."""
    lower = chip_name.lower()
    if "nearmap_north" in lower:
        return "Nearmap North (oblique)"
    if "nearmap_east" in lower:
        return "Nearmap East (oblique)"
    if "nearmap_south" in lower:
        return "Nearmap South (oblique)"
    if "nearmap_west" in lower:
        return "Nearmap West (oblique)"
    if "nearmap_vert" in lower:
        return "Nearmap Vert (top-down)"
    if "naip_wide" in lower:
        return "NAIP wide (top-down)"
    if "naip" in lower and "nearmap" not in lower:
        return "NAIP (top-down)"
    if "_zoom_" in lower:
        suffix = lower.rsplit("_zoom_", 1)[-1].split(".")[0]
        return f"Zoom crop {suffix}"
    return Path(chip_name).stem


def _prioritize_chips(
    chips: Sequence[str], *, asset_view: str | None
) -> list[str]:
    """Put the boxed asset view and zoom crops first for review."""
    boxed: list[str] = []
    zooms: list[str] = []
    rest: list[str] = []
    for name in chips:
        lower = name.lower()
        if chip_matches_asset_view(name, asset_view):
            boxed.append(name)
        elif "_zoom_" in lower:
            zooms.append(name)
        else:
            rest.append(name)
    return boxed + zooms + rest


def _render_chip_html(
    name: str,
    *,
    asset_view: str | None,
    box: list[int] | None,
) -> str:
    src = html.escape(f"../chips/{name}")
    safe_name = html.escape(name)
    title = chip_display_title(name)
    safe_title = html.escape(title)
    is_boxed = bool(box) and chip_matches_asset_view(name, asset_view)
    is_zoom = "_zoom_" in name.lower()
    badges: list[str] = []
    box_html = ""
    classes = ["chip"]
    if is_boxed and box is not None:
        classes.append("has-box")
        badges.append("lime box = model asset_box on THIS view only")
        ymin, xmin, ymax, xmax = box
        top = ymin / 10.0
        left = xmin / 10.0
        height = (ymax - ymin) / 10.0
        width = (xmax - xmin) / 10.0
        box_html = (
            f'<span class="asset-box" style="top:{top:.2f}%;left:{left:.2f}%;'
            f'height:{height:.2f}%;width:{width:.2f}%;"></span>'
        )
    elif is_zoom:
        classes.append("is-zoom")
        badges.append("zoom crop")
    badge_html = (
        f'<span class="chip-badge">{html.escape(" · ".join(badges))}</span>'
        if badges
        else ""
    )
    return (
        f'<a class="{" ".join(classes)}" href="{src}" target="_blank" '
        f'rel="noopener" title="{safe_name}">'
        f'<span class="chip-frame">'
        f'<img src="{src}" alt="{safe_title}" loading="lazy"/>'
        f"{box_html}"
        f"</span>"
        f'<span class="chip-title">{safe_title}</span>'
        f"{badge_html}"
        f"</a>"
    )


def _render_index_html(*, run_dir: Path, rows: Sequence[dict[str, Any]]) -> str:
    def card_html(row: dict[str, Any], *, allow_approve: bool) -> str:
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
        asset_view = str(row.get("asset_view") or "").strip() or None
        box = parse_asset_box(row.get("asset_box_2d"))
        chips = _prioritize_chips(
            [c for c in str(row.get("chip_links") or "").split(";") if c],
            asset_view=asset_view,
        )
        imgs = [
            _render_chip_html(name, asset_view=asset_view, box=box)
            for name in chips[:8]
        ]
        evidence = html.escape(str(row.get("cell_equipment_evidence") or "")[:400])
        site_ev = html.escape(str(row.get("site_evidence") or "")[:300])
        reason = html.escape(str(row.get("holdout_reason") or "") or "—")
        box_meta = (
            f"box={html.escape(str(row.get('asset_box_2d') or '—'))} · "
            f"view={html.escape(asset_view or '—')} "
            f"(lime outline only on that view)"
        )
        gemini_cell = str(row.get("gemini_cell_equipment") or "").strip() or "—"
        claude_cell = str(row.get("claude_cell_equipment") or "").strip() or "—"
        agree_raw = str(row.get("cell_models_agree") or "").strip().lower()
        agree_cls = (
            "agree-yes"
            if agree_raw in {"true", "1", "yes"}
            else "agree-no"
            if agree_raw in {"false", "0", "no"}
            else "agree-na"
        )
        models = (
            f'<span class="model-pill gemini">Gemini cell={html.escape(gemini_cell)}</span> '
            f'<span class="model-pill claude">Claude cell={html.escape(claude_cell)}</span> '
            f'<span class="model-pill {agree_cls}">agree={html.escape(str(row.get("cell_models_agree") or "—"))}</span>'
        )
        meta = (
            f"type={html.escape(str(row.get('update_site_type') or row.get('naip_site_type') or ''))} · "
            f"cell={html.escape(str(row.get('naip_cell_equipment') or ''))} · "
            f"cell_conf={html.escape(str(row.get('cell_equipment_confidence') or ''))} · "
            f"tier={html.escape(str(row.get('nearmap_tier') or ''))} · "
            f"holdout={reason} · {box_meta}"
        )
        conflict = ""
        site_l = str(row.get("site_evidence") or "").lower()
        cell_false = str(row.get("naip_cell_equipment") or "").lower() in {
            "false",
            "0",
            "no",
        }
        if cell_false and any(
            cue in site_l
            for cue in ("sector", "antenna", "cellular", "cell site", "panel")
        ):
            conflict = (
                '<p class="conflict">Site write-up claims cell gear but final '
                "cell call is false — trust Cell evidence + the view titles "
                "below, not Site evidence alone.</p>"
            )
        approve = (
            f"""<label class="approve">
      <input type="checkbox" class="approve-box" data-id="{sid}"/>
      Approve
    </label>"""
            if allow_approve
            else '<div class="approve muted">Holdout</div>'
        )
        return f"""
<article class="card" data-id="{sid}">
  <header>
    {approve}
    <div>
      <h2>{sid}</h2>
      <p class="addr">{street or "—"}</p>
      <p class="models">{models}</p>
      <p class="meta">{meta}</p>
    </div>
  </header>
  {conflict}
  <p class="evidence"><strong>Cell evidence (final cell call / Claude or crop):</strong> {evidence or "—"}</p>
  <p class="evidence"><strong>Site evidence (structure write-up; can lag behind cell):</strong> {site_ev or "—"}</p>
  <div class="chips">{"".join(imgs) or "<p>No chips</p>"}</div>
</article>
"""

    candidates = [r for r in rows if r.get("review_section") != "rooftop_holdout"]
    holdouts = [r for r in rows if r.get("review_section") == "rooftop_holdout"]
    candidate_cards = "".join(card_html(r, allow_approve=True) for r in candidates)
    holdout_cards = "".join(card_html(r, allow_approve=False) for r in holdouts)

    main_parts = [
        f"<section><h1>SF candidates ({len(candidates)})</h1>"
        f"{candidate_cards or '<p>No potential_update candidates.</p>'}</section>",
        f"<section><h1>Rooftop holdouts ({len(holdouts)})</h1>"
        f"<p class='section-hint'>Model called rooftop but cell gear did not clear "
        f"gates — visual QA only (not pushed by --apply-reviewed). "
        f"Lime outline = model asset_box_2d on that view; "
        f"zoom crops are magnified regions from classify.</p>"
        f"{holdout_cards or '<p>No rooftop holdouts.</p>'}</section>",
    ]

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
    main {{ padding: 20px; max-width: 1200px; margin: 0 auto; display: grid; gap: 28px; }}
    section h1 {{ margin: 0 0 8px; font-size: 18px; }}
    .section-hint {{ margin: 0 0 12px; font-size: 13px; color: #555; }}
    .card {{ background: #fff; border: 1px solid #d8d8d2; padding: 16px; margin-bottom: 16px; }}
    .card header {{ display: flex; gap: 16px; align-items: flex-start; }}
    .approve {{ display: flex; gap: 8px; align-items: center; font-weight: 700;
      min-width: 110px; }}
    .approve.muted {{ color: #777; font-weight: 600; }}
    h2 {{ margin: 0 0 4px; font-size: 16px; }}
    .addr, .meta, .evidence, .models {{ margin: 4px 0; font-size: 13px; color: #333; }}
    .models {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
    .model-pill {{ display: inline-block; padding: 2px 8px; font-size: 12px;
      font-weight: 700; border: 1px solid #ccc; background: #f0f0ec; }}
    .model-pill.gemini {{ border-color: #4285f4; color: #1a53b8; background: #e8f0fe; }}
    .model-pill.claude {{ border-color: #d97706; color: #9a3412; background: #fff7ed; }}
    .model-pill.agree-yes {{ border-color: #16a34a; color: #166534; background: #dcfce7; }}
    .model-pill.agree-no {{ border-color: #dc2626; color: #991b1b; background: #fee2e2; }}
    .model-pill.agree-na {{ border-color: #9ca3af; color: #4b5563; background: #f3f4f6; }}
    .chips {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: 10px; margin-top: 12px; }}
    a.chip {{ display: block; text-decoration: none; color: inherit;
      border: 1px solid #d8d8d2; background: #fafaf7; }}
    a.chip.has-box {{ border-color: #1bbf5a; box-shadow: 0 0 0 1px #1bbf5a; }}
    a.chip.is-zoom {{ border-color: #3b82f6; }}
    .chip-frame {{ position: relative; display: block; background: #111; }}
    .chip-frame img {{ width: 100%; height: auto; vertical-align: top;
      display: block; background: #222; }}
    .asset-box {{ position: absolute; border: 2px solid #39ff8a;
      box-shadow: 0 0 0 1px rgba(0,0,0,.65), inset 0 0 0 1px rgba(0,0,0,.25);
      pointer-events: none; }}
    .chip-title {{ display: block; padding: 6px 8px 2px; font-size: 12px;
      font-weight: 700; color: #111; background: #fafaf7; }}
    .chip-badge {{ display: block; padding: 0 8px 6px; font-size: 11px;
      font-weight: 600; color: #1bbf5a; background: #fafaf7; }}
    a.chip.is-zoom .chip-badge {{ color: #2563eb; }}
    .conflict {{ margin: 8px 0; padding: 8px 10px; font-size: 13px;
      background: #fff7ed; border: 1px solid #fdba74; color: #9a3412; }}
    .hint {{ font-size: 13px; opacity: 0.85; }}
  </style>
</head>
<body>
  <header class="top">
    <div>
      <div><strong>Review</strong> — {html.escape(run_dir.name)}</div>
      <div class="hint">Approve SF candidates to push. Lime box only on the named asset_view chip. Titles under each image are the view names cited in evidence.</div>
    </div>
    <div>
      <span id="count">0 approved</span>
      &nbsp;
      <button type="button" id="download">Download review_manifest.csv</button>
    </div>
  </header>
  <main>
    {"".join(main_parts)}
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
