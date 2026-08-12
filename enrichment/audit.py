"""Post-classify / post-apply spot audit sample of ready candidates."""

from __future__ import annotations

import csv
import hashlib
import html
from pathlib import Path
from typing import Any, Sequence

from enrichment.constants import (
    BUCKET_POTENTIAL_UPDATE,
    CANDIDATE_CSV,
    DETAIL_CSV,
    MATCH_SOURCE_NONE,
    REVIEW_DIR_NAME,
    SPOT_AUDIT_CSV,
    SPOT_AUDIT_HTML,
    SPOT_AUDIT_MAX,
    SPOT_AUDIT_MIN,
    SPOT_AUDIT_RATE,
)


def _stable_unit(site_id: str) -> float:
    digest = hashlib.sha1(site_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def select_spot_audit_sample(
    candidates: Sequence[dict[str, Any]],
    *,
    rate: float = SPOT_AUDIT_RATE,
    min_n: int = SPOT_AUDIT_MIN,
    max_n: int = SPOT_AUDIT_MAX,
    prefer_imagery_only: bool = True,
) -> list[dict[str, Any]]:
    """Deterministic ~rate sample of ready candidates for human spot check.

    Preferentially includes imagery-only writes (highest FP risk), then fills
    from DB-backed candidates. Always returns between min_n and max_n when
    enough candidates exist (or all candidates if fewer than min_n).
    """
    rows = [
        dict(r)
        for r in candidates
        if str(r.get("Id") or "").strip()
        and str(r.get("bucket") or BUCKET_POTENTIAL_UPDATE) == BUCKET_POTENTIAL_UPDATE
    ]
    if not rows:
        return []

    target = max(min_n, min(max_n, int(round(len(rows) * max(0.0, rate)))))
    target = min(max(target, min(min_n, len(rows))), len(rows), max_n)

    def _risk_key(row: dict[str, Any]) -> tuple[int, float, str]:
        sid = str(row.get("Id") or "")
        imagery_only = (
            str(row.get("match_source") or MATCH_SOURCE_NONE) == MATCH_SOURCE_NONE
        )
        # 0 = prefer first (imagery-only when prefer_imagery_only).
        tier = 0 if (prefer_imagery_only and imagery_only) else 1
        return (tier, _stable_unit(sid), sid)

    ranked = sorted(rows, key=_risk_key)
    # Take all imagery-only first up to target, then DB-backed.
    return ranked[:target]


def write_spot_audit_package(
    run_dir: Path,
    candidates: Sequence[dict[str, Any]] | None = None,
    *,
    applied_ids: Sequence[str] | None = None,
    rate: float = SPOT_AUDIT_RATE,
) -> Path:
    """Write review/spot_audit.html + .csv. Returns the review dir path."""
    run_dir = Path(run_dir)
    review_dir = run_dir / REVIEW_DIR_NAME
    review_dir.mkdir(parents=True, exist_ok=True)

    if candidates is None:
        path = run_dir / CANDIDATE_CSV
        if not path.exists():
            path = run_dir / DETAIL_CSV
        with path.open(encoding="utf-8-sig", newline="") as handle:
            loaded = list(csv.DictReader(handle))
        candidates = [
            r
            for r in loaded
            if str(r.get("bucket") or "") in {"", BUCKET_POTENTIAL_UPDATE}
            and (
                str(r.get("bucket") or "") == BUCKET_POTENTIAL_UPDATE
                or path.name == CANDIDATE_CSV
            )
        ]

    sample = select_spot_audit_sample(candidates, rate=rate)
    if applied_ids is not None:
        applied = {str(x).strip() for x in applied_ids if str(x).strip()}
        if applied:
            sample = [r for r in sample if str(r.get("Id") or "") in applied] or sample

    fields = [
        "Id",
        "match_source",
        "update_site_type",
        "update_coord_source",
        "dual_model_resolution",
        "escalation_model",
        "nearmap_tier",
        "asset_view",
        "cell_equipment_confidence",
        "Site_Street__c",
        "Site_City__c",
        "Site_State__c",
        "audit_note",
    ]
    csv_path = review_dir / SPOT_AUDIT_CSV
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in sample:
            out = {k: row.get(k, "") for k in fields}
            out["audit_note"] = ""
            writer.writerow(out)

    chip_dir = run_dir / "chips"
    cards: list[str] = []
    for row in sample:
        sid = html.escape(str(row.get("Id") or ""))
        addr = html.escape(
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
        meta = html.escape(
            " | ".join(
                [
                    f"type={row.get('update_site_type') or '—'}",
                    f"match={row.get('match_source') or 'none'}",
                    f"coord={row.get('update_coord_source') or '—'}",
                    f"dual={row.get('dual_model_resolution') or '—'}",
                    f"tier={row.get('nearmap_tier') or '—'}",
                ]
            )
        )
        chips = []
        if chip_dir.exists():
            for path in sorted(chip_dir.glob(f"{sid}_*.jpg"))[:6]:
                rel = html.escape(f"../chips/{path.name}")
                chips.append(
                    f'<a href="{rel}" target="_blank">'
                    f'<img src="{rel}" alt="{html.escape(path.name)}" '
                    f'style="max-width:160px;max-height:120px;margin:4px;"/></a>'
                )
        chip_html = "".join(chips) or "<em>no chips</em>"
        cards.append(
            f"<section class='card'><h3>{sid}</h3>"
            f"<p>{addr}</p><p class='meta'>{meta}</p>"
            f"<div class='chips'>{chip_html}</div></section>"
        )

    body = (
        "\n".join(cards)
        if cards
        else "<p>No ready candidates to spot-audit in this run.</p>"
    )
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Spot audit — {html.escape(run_dir.name)}</title>
  <style>
    body {{ font-family: Segoe UI, sans-serif; margin: 24px; background: #f6f7f9; }}
    h1 {{ margin-bottom: 4px; }}
    .sub {{ color: #555; margin-bottom: 20px; }}
    .card {{ background: #fff; border: 1px solid #ddd; padding: 12px 16px;
             margin-bottom: 16px; border-radius: 6px; }}
    .meta {{ font-size: 13px; color: #444; }}
    .chips img {{ border: 1px solid #ccc; }}
  </style>
</head>
<body>
  <h1>Spot audit</h1>
  <p class="sub">
    Deterministic ~{rate:.0%} sample of ready candidates
    (prefer imagery-only). QA these even when auto-apply succeeded.
    Sample size: {len(sample)} / {len(list(candidates or []))}.
    Manifest: {SPOT_AUDIT_CSV}
  </p>
  {body}
</body>
</html>
"""
    (review_dir / SPOT_AUDIT_HTML).write_text(html_doc, encoding="utf-8")
    return review_dir
