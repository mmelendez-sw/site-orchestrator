"""Build a human review package for enrichment candidates before SF apply."""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any, Sequence

from enrichment.constants import (
    BUCKET_OTHER,
    BUCKET_POTENTIAL_UPDATE,
    BUCKET_ROOFTOP,
    CANDIDATE_CSV,
    DETAIL_CSV,
    REVIEW_DIR_NAME,
    REVIEW_INDEX_HTML,
    REVIEW_MANIFEST_CSV,
)

# Review UI sections (not the same as bucketing holdout_reason).
REVIEW_SECTION_READY = "ready"
REVIEW_SECTION_CONTENTION = "contention"
REVIEW_SECTION_NO_CELL = "no_cell"

# Pipeline said rooftop/cell-ish but gates blocked → human judgment.
_CONTENTION_REASONS = frozenset(
    {
        "rooftop_needs_dual_model_cell",
        "rooftop_low_cell_confidence",
        "rooftop_needs_oblique_asset_box",
        "rooftop_needs_asset_box",
        "rooftop_view_evidence_mismatch",
        "rooftop_needs_nearmap_obliques",
        "rooftop_no_telecom_evidence",
        "rooftop_naip_only_forbidden",
        "rooftop_naip_verified_forbidden",
        "low_confidence_imagery_only",
        "tower_needs_dual_model_cell",
        "tower_needs_nearmap_obliques",
        "tower_needs_asset_box",
        "imagery_only_needs_crop_or_localize_agree",
    }
)

_NO_CELL_REASONS = frozenset(
    {
        "rooftop_no_cell_equipment",
        "tower_no_cell_equipment",
        "other",
        "unmapped_site_type",
    }
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
    "classify_lat",
    "classify_lng",
    "classify_coord_source",
    "pin_address_offset_m",
    "pin_address_mismatch",
    "address_lat",
    "address_lng",
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
    "dual_model_resolution",
    "asset_offset_m",
    "asset_box_2d",
    "asset_view",
    "Site_Street__c",
    "Site_City__c",
    "Site_State__c",
    "chip_links",
)


def _truthy_cell(value: Any) -> bool | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def classify_review_section(row: dict[str, Any]) -> str:
    """Map an enrichment detail row into ready / contention / no_cell."""
    bucket = str(row.get("bucket") or "").strip()
    if bucket == BUCKET_POTENTIAL_UPDATE:
        return REVIEW_SECTION_READY

    reason = str(row.get("holdout_reason") or "").strip()
    gemini = _truthy_cell(row.get("gemini_cell_equipment"))
    claude = _truthy_cell(row.get("claude_cell_equipment"))
    final_cell = _truthy_cell(row.get("naip_cell_equipment"))
    agree = _truthy_cell(row.get("cell_models_agree"))
    resolution = str(row.get("dual_model_resolution") or "").strip().lower()

    # Model conflict or soft-keep: human must decide (lime box often misleading).
    if agree is False or resolution in {"claude_veto", "soft_keep_gemini"}:
        return REVIEW_SECTION_CONTENTION
    if gemini is True and claude is False:
        return REVIEW_SECTION_CONTENTION
    if gemini is True and final_cell is not True:
        return REVIEW_SECTION_CONTENTION

    if reason in _CONTENTION_REASONS or reason.startswith("asset_offset_"):
        return REVIEW_SECTION_CONTENTION
    if final_cell is True and bucket == BUCKET_ROOFTOP:
        return REVIEW_SECTION_CONTENTION

    if reason in _NO_CELL_REASONS or reason.startswith("else:"):
        return REVIEW_SECTION_NO_CELL
    if final_cell is False:
        return REVIEW_SECTION_NO_CELL
    if bucket == BUCKET_OTHER:
        return REVIEW_SECTION_NO_CELL
    if bucket == BUCKET_ROOFTOP and final_cell is not True:
        return REVIEW_SECTION_NO_CELL

    # Errors / unclear leftovers → contention for a second look.
    return REVIEW_SECTION_CONTENTION


def write_review_package(
    run_dir: Path,
    *,
    candidates: Sequence[dict[str, Any]] | None = None,
    rooftop_holdouts: Sequence[dict[str, Any]] | None = None,
) -> Path:
    """Write run_dir/review/ with manifest CSV + index.html for visual QA.

    Sections:
      ready — pipeline candidates (Approve to push)
      contention — dual-model conflict / borderline gates
      no_cell — no cellular equipment / not a cell site
    """
    run_dir = Path(run_dir)
    review_dir = run_dir / REVIEW_DIR_NAME
    review_dir.mkdir(parents=True, exist_ok=True)

    if candidates is None and rooftop_holdouts is None:
        source_rows = _load_all_review_rows(run_dir)
    else:
        by_id: dict[str, dict[str, Any]] = {}
        for row in list(candidates or []) + list(rooftop_holdouts or []):
            sid = str(row.get("Id") or "").strip()
            if sid:
                by_id[sid] = dict(row)
        for row in _load_all_review_rows(run_dir):
            sid = str(row.get("Id") or "").strip()
            if not sid:
                continue
            if sid not in by_id:
                by_id[sid] = row
            else:
                by_id[sid] = {**row, **by_id[sid]}
        source_rows = list(by_id.values())

    chip_dir = run_dir / "chips"
    rows: list[dict[str, Any]] = []
    for row in source_rows:
        sid = str(row.get("Id") or "").strip()
        if not sid:
            continue
        chips = _chip_names_for_id(chip_dir, sid)
        section = classify_review_section(row)
        entry = {
            col: row.get(col, "")
            for col in REVIEW_MANIFEST_COLUMNS
            if col not in {"approved", "review_section", "chip_links"}
        }
        entry["approved"] = ""
        entry["review_section"] = section
        entry["bucket"] = row.get("bucket") or ""
        entry["holdout_reason"] = row.get("holdout_reason") or ""
        entry["chip_links"] = ";".join(chips)
        for col in REVIEW_MANIFEST_COLUMNS:
            entry.setdefault(col, "")
        rows.append(entry)

    section_order = {
        REVIEW_SECTION_READY: 0,
        REVIEW_SECTION_CONTENTION: 1,
        REVIEW_SECTION_NO_CELL: 2,
    }
    rows.sort(
        key=lambda r: (
            section_order.get(str(r.get("review_section")), 9),
            str(r.get("Id") or ""),
        )
    )

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
                "2. Ready to approve: pipeline-cleared cellular sites — check Approve to push.",
                "3. In contention: Gemini/Claude disagree or borderline gates — visual QA;",
                "   lime boxes here are often Gemini HVAC false positives.",
                "4. No cell equipment: model did not confirm cellular gear / not a cell site.",
                "5. Download review_manifest.csv and replace this folder's copy.",
                "6. Push approved ready sites:",
                "   python -m enrichment --apply-reviewed --run-dir <this-run>",
                "7. Auto-apply path: --apply --dequeue-holdouts (Claude hard-agree only).",
                "8. After classify/apply, open spot_audit.html (~10% sample, imagery-only first).",
                "9. Holdout triage: ../holdout_triage.md (top reasons + weekly focus).",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return review_dir


def load_approved_ids(review_dir: Path, *, approve_all: bool = False) -> set[str]:
    """Return Salesforce Ids marked approved=yes in the review manifest.

    With approve_all, only the ready/candidate section is auto-selected.
    """
    manifest = Path(review_dir) / REVIEW_MANIFEST_CSV
    if not manifest.exists():
        raise FileNotFoundError(f"Review manifest not found: {manifest}")
    approved: set[str] = set()
    with manifest.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            sid = str(row.get("Id") or "").strip()
            if not sid:
                continue
            section = str(row.get("review_section") or "").strip()
            if approve_all:
                if section in {
                    REVIEW_SECTION_CONTENTION,
                    REVIEW_SECTION_NO_CELL,
                    "rooftop_holdout",
                }:
                    continue
                if section in {REVIEW_SECTION_READY, "candidate", ""}:
                    approved.add(sid)
                elif str(row.get("bucket") or "") == BUCKET_POTENTIAL_UPDATE:
                    approved.add(sid)
                continue
            flag = str(row.get("approved") or "").strip().lower()
            if flag in {"yes", "y", "true", "1", "approve", "approved"}:
                approved.add(sid)
    return approved


def _load_all_review_rows(run_dir: Path) -> list[dict[str, Any]]:
    """Load detail rows worth showing in review (candidates + rooftops + other)."""
    detail_csv = run_dir / DETAIL_CSV
    detail_by_id: dict[str, dict[str, Any]] = {}
    if detail_csv.exists():
        with detail_csv.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                detail_by_id[str(row.get("Id") or "")] = row

    candidate_csv = run_dir / CANDIDATE_CSV
    if candidate_csv.exists():
        with candidate_csv.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sid = str(row.get("Id") or "")
                detail_by_id[sid] = {**(detail_by_id.get(sid) or {}), **row}

    keep_buckets = {BUCKET_POTENTIAL_UPDATE, BUCKET_ROOFTOP, BUCKET_OTHER}
    return [
        r
        for r in detail_by_id.values()
        if str(r.get("bucket") or "") in keep_buckets
    ]


def _load_review_rows(
    run_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Legacy split kept for callers; prefer _load_all_review_rows."""
    rows = _load_all_review_rows(run_dir)
    candidates = [r for r in rows if r.get("bucket") == BUCKET_POTENTIAL_UPDATE]
    holdouts = [r for r in rows if r.get("bucket") == BUCKET_ROOFTOP]
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
        if "_pin_nearmap_vert" in lower or lower.startswith("pin_") or "_pin_" in lower:
            return "SF pin Vert (comparison)"
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


def cell_verdict_for_row(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return (css_class, short_label, detail) for the card verdict banner."""
    section = str(row.get("review_section") or "")
    if section == REVIEW_SECTION_READY:
        return (
            "verdict-cell-yes",
            "CELL EQUIPMENT",
            "Pipeline-cleared cellular gear — safe to Approve for Salesforce.",
        )
    if section == REVIEW_SECTION_NO_CELL:
        return (
            "verdict-cell-no",
            "NO CELL EQUIPMENT",
            "No confirmed cellular gear (or site is other / not a cell site).",
        )
    return (
        "verdict-cell-unclear",
        "CELL EQUIPMENT UNCERTAIN",
        "Models disagree or gates blocked — do not treat any box as confirmed.",
    )


def _render_chip_html(
    name: str,
    *,
    asset_view: str | None,
    box: list[int] | None,
    box_trust: str | None = None,
) -> str:
    """box_trust: 'confirmed' | 'untrusted' | None (hide overlay)."""
    src = html.escape(f"../chips/{name}")
    safe_name = html.escape(name)
    title = chip_display_title(name)
    safe_title = html.escape(title)
    is_boxed = bool(box) and chip_matches_asset_view(name, asset_view)
    is_zoom = "_zoom_" in name.lower()
    badges: list[str] = []
    box_html = ""
    classes = ["chip"]
    if is_boxed and box is not None and box_trust == "confirmed":
        classes.append("has-box")
        badges.append("CONFIRMED cell box on THIS view")
        ymin, xmin, ymax, xmax = box
        top = ymin / 10.0
        left = xmin / 10.0
        height = (ymax - ymin) / 10.0
        width = (xmax - xmin) / 10.0
        box_html = (
            f'<span class="asset-box asset-box-confirmed" '
            f'style="top:{top:.2f}%;left:{left:.2f}%;'
            f'height:{height:.2f}%;width:{width:.2f}%;"></span>'
        )
    elif is_boxed and box is not None and box_trust == "untrusted":
        classes.append("has-box-untrusted")
        badges.append("UNCONFIRMED model claim — often HVAC, not cell")
        ymin, xmin, ymax, xmax = box
        top = ymin / 10.0
        left = xmin / 10.0
        height = (ymax - ymin) / 10.0
        width = (xmax - xmin) / 10.0
        box_html = (
            f'<span class="asset-box asset-box-untrusted" '
            f'style="top:{top:.2f}%;left:{left:.2f}%;'
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
        section = str(row.get("review_section") or "")
        verdict_cls, verdict_label, verdict_detail = cell_verdict_for_row(row)
        # Only ready gets confirmed lime; contention shows untrusted orange;
        # no_cell hides any leftover model box.
        if section == REVIEW_SECTION_READY:
            box_trust: str | None = "confirmed"
        elif section == REVIEW_SECTION_CONTENTION:
            box_trust = "untrusted"
        else:
            box_trust = None
        asset_view = str(row.get("asset_view") or "").strip() or None
        box = parse_asset_box(row.get("asset_box_2d")) if box_trust else None
        chips = _prioritize_chips(
            [c for c in str(row.get("chip_links") or "").split(";") if c],
            asset_view=asset_view if box_trust else None,
        )
        imgs = [
            _render_chip_html(
                name,
                asset_view=asset_view if box_trust else None,
                box=box,
                box_trust=box_trust,
            )
            for name in chips[:8]
        ]
        evidence = html.escape(str(row.get("cell_equipment_evidence") or "")[:400])
        site_ev = html.escape(str(row.get("site_evidence") or "")[:300])
        reason = html.escape(str(row.get("holdout_reason") or "") or "—")
        if box_trust == "confirmed" and box:
            box_meta = (
                f"confirmed box on {html.escape(asset_view or '—')} · "
                f"{html.escape(str(row.get('asset_box_2d') or ''))}"
            )
        elif box_trust == "untrusted" and parse_asset_box(row.get("asset_box_2d")):
            box_meta = (
                f"UNCONFIRMED box claim on "
                f"{html.escape(str(row.get('asset_view') or '—'))} — "
                f"often HVAC false positive"
            )
        else:
            box_meta = "no asset box shown"
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
        final_cell = str(row.get("naip_cell_equipment") or "").strip() or "—"
        pin_addr = str(row.get("pin_address_offset_m") or "").strip()
        pin_mis = str(row.get("pin_address_mismatch") or "").strip().lower() in {
            "true",
            "1",
            "yes",
        }
        pin_note = ""
        if pin_addr:
            pin_note = (
                f"pin↔address={html.escape(pin_addr)}m"
                + (" MISMATCH→classified at address" if pin_mis else "")
                + " · "
            )
        meta = (
            f"{pin_note}"
            f"final cell={html.escape(final_cell)} · "
            f"type={html.escape(str(row.get('update_site_type') or row.get('naip_site_type') or ''))} · "
            f"cell_conf={html.escape(str(row.get('cell_equipment_confidence') or ''))} · "
            f"tier={html.escape(str(row.get('nearmap_tier') or ''))} · "
            f"reason={reason} · {box_meta}"
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
                '<p class="conflict">Site write-up mentions cell-like language but '
                "final cell call is false — trust the verdict banner and Cell "
                "evidence, not Site evidence alone.</p>"
            )
        badge = {
            REVIEW_SECTION_READY: "Approve",
            REVIEW_SECTION_CONTENTION: "Do not approve",
            REVIEW_SECTION_NO_CELL: "No cell",
        }.get(section, "Review")
        approve = (
            f"""<label class="approve">
      <input type="checkbox" class="approve-box" data-id="{sid}"/>
      Approve
    </label>"""
            if allow_approve
            else f'<div class="approve muted">{badge}</div>'
        )
        return f"""
<article class="card {verdict_cls}" data-id="{sid}">
  <div class="verdict-banner {verdict_cls}">
    <span class="verdict-label">{html.escape(verdict_label)}</span>
    <span class="verdict-detail">{html.escape(verdict_detail)}</span>
  </div>
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
  <p class="evidence"><strong>Cell evidence:</strong> {evidence or "—"}</p>
  <p class="evidence"><strong>Site evidence (structure only):</strong> {site_ev or "—"}</p>
  <div class="chips">{"".join(imgs) or "<p>No chips</p>"}</div>
</article>
"""

    ready = [r for r in rows if r.get("review_section") == REVIEW_SECTION_READY]
    contention = [
        r for r in rows if r.get("review_section") == REVIEW_SECTION_CONTENTION
    ]
    no_cell = [r for r in rows if r.get("review_section") == REVIEW_SECTION_NO_CELL]
    # Legacy manifests used candidate / rooftop_holdout.
    if not ready and not contention and not no_cell:
        ready = [r for r in rows if r.get("review_section") != "rooftop_holdout"]
        contention = [r for r in rows if r.get("review_section") == "rooftop_holdout"]

    ready_cards = "".join(card_html(r, allow_approve=True) for r in ready)
    contention_cards = "".join(
        card_html(r, allow_approve=False) for r in contention
    )
    no_cell_cards = "".join(card_html(r, allow_approve=False) for r in no_cell)

    main_parts = [
        f"<section class='sec-ready'><h1>CELL EQUIPMENT — ready to approve "
        f"({len(ready)})</h1>"
        f"<p class='section-hint'>Green = confirmed cellular (Claude hard-agree). "
        f"With --apply these auto-push; otherwise Approve + --apply-reviewed. "
        f"Lime box is only on the named "
        f"asset view.</p>"
        f"{ready_cards or '<p>No ready candidates.</p>'}</section>",
        f"<section class='sec-contention'><h1>CELL EQUIPMENT UNCERTAIN — in "
        f"contention ({len(contention)})</h1>"
        f"<p class='section-hint'>Red/orange = not confirmed. Gemini/Claude "
        f"disagree, soft-keep, or borderline gates. Orange boxes are unconfirmed "
        f"model claims (often HVAC). Visual QA only; dequeue with "
        f"--dequeue-holdouts.</p>"
        f"{contention_cards or '<p>No contention sites.</p>'}</section>",
        f"<section class='sec-no-cell'><h1>NO CELL EQUIPMENT ({len(no_cell)})</h1>"
        f"<p class='section-hint'>Gray = no confirmed cellular gear, or site is "
        f"other/not a cell site. No asset boxes shown. Dequeue with "
        f"--dequeue-holdouts.</p>"
        f"{no_cell_cards or '<p>No no-cell sites.</p>'}</section>",
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
    .sec-ready h1 {{ color: #166534; }}
    .sec-contention h1 {{ color: #991b1b; }}
    .sec-no-cell h1 {{ color: #4b5563; }}
    .card {{ background: #fff; border: 1px solid #d8d8d2; border-left-width: 6px;
      padding: 0 16px 16px; margin-bottom: 16px; overflow: hidden; }}
    .card.verdict-cell-yes {{ border-left-color: #16a34a; }}
    .card.verdict-cell-no {{ border-left-color: #6b7280; }}
    .card.verdict-cell-unclear {{ border-left-color: #dc2626; }}
    .verdict-banner {{ display: flex; flex-wrap: wrap; gap: 8px 16px;
      align-items: baseline; margin: 0 -16px 12px; padding: 10px 16px;
      font-size: 13px; }}
    .verdict-banner.verdict-cell-yes {{ background: #dcfce7; color: #14532d; }}
    .verdict-banner.verdict-cell-no {{ background: #e5e7eb; color: #1f2937; }}
    .verdict-banner.verdict-cell-unclear {{ background: #fee2e2; color: #7f1d1d; }}
    .verdict-label {{ font-size: 15px; font-weight: 800; letter-spacing: 0.02em; }}
    .verdict-detail {{ opacity: 0.95; }}
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
    a.chip.has-box-untrusted {{ border-color: #ea580c; box-shadow: 0 0 0 1px #ea580c; }}
    a.chip.is-zoom {{ border-color: #3b82f6; }}
    .chip-frame {{ position: relative; display: block; background: #111; }}
    .chip-frame img {{ width: 100%; height: auto; vertical-align: top;
      display: block; background: #222; }}
    .asset-box {{ position: absolute; pointer-events: none; }}
    .asset-box-confirmed {{ border: 2px solid #39ff8a;
      box-shadow: 0 0 0 1px rgba(0,0,0,.65), inset 0 0 0 1px rgba(0,0,0,.25); }}
    .asset-box-untrusted {{ border: 2px dashed #ea580c;
      box-shadow: 0 0 0 1px rgba(0,0,0,.55); }}
    .chip-title {{ display: block; padding: 6px 8px 2px; font-size: 12px;
      font-weight: 700; color: #111; background: #fafaf7; }}
    .chip-badge {{ display: block; padding: 0 8px 6px; font-size: 11px;
      font-weight: 600; color: #1bbf5a; background: #fafaf7; }}
    a.chip.has-box-untrusted .chip-badge {{ color: #c2410c; }}
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
      <div class="hint">Green CELL EQUIPMENT = approve. Red UNCERTAIN = ignore boxes.
        Gray NO CELL = no cellular gear. Only ready cards can be checked Approve.</div>
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
