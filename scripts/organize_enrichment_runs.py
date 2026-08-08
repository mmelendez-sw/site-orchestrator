"""Reorganize runs/ by pipeline process theme (not street-prefix filters).

Themes:
  enrichment/sf_blank_site_type_naip_aug2026  — Aug 2026 blank Site_Type → FCC/TS → NAIP → SF update
  enrichment/aborts_smoke                     — empty / tiny launches
  pipeline_e2e                                — CSV → dedupe → classify → SF create (placeholder if none)
  csv_sf_upload                               — sf_upload.csv create uploads (placeholder if none)

Street-digit query filters were batching tactics inside one enrichment campaign;
they are NOT separate themes.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

RUNS = Path("runs")
ENRICH_ROOT = RUNS / "enrichment"
THEME_MAIN = ENRICH_ROOT / "sf_blank_site_type_naip_aug2026"
THEME_ABORTS = ENRICH_ROOT / "aborts_smoke"
PIPELINE = RUNS / "pipeline_e2e"
CSV_UPLOAD = RUNS / "csv_sf_upload"
ARTIFACTS = RUNS / "_artifacts"


def _is_enrichment_run(path: Path) -> bool:
    return path.is_dir() and path.name.endswith("_sf_enrichment")


def _iter_enrichment_runs() -> list[Path]:
    return sorted(p for p in RUNS.rglob("*_sf_enrichment") if _is_enrichment_run(p))


def _detail_n(run: Path) -> int:
    detail = run / "enrichment_detail.csv"
    if not detail.exists():
        return 0
    # count data lines cheaply
    with detail.open(encoding="utf-8-sig", errors="ignore") as handle:
        lines = sum(1 for _ in handle)
    return max(0, lines - 1)


def _move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if src.resolve() == dest.resolve():
        return dest
    if dest.exists():
        raise FileExistsError(dest)
    shutil.move(str(src), str(dest))
    return dest


def _classify_pipeline_signals(path: Path) -> str | None:
    """Detect non-enrichment run folders by artifacts."""
    if (path / "dedupe_results.csv").exists() or (path / "sf_upload.csv").exists():
        if (path / "classify_detail.csv").exists() or (path / "chips").exists():
            return "pipeline_e2e"
        return "csv_sf_upload"
    if (path / "classify_detail.csv").exists() and not (
        path / "enrichment_detail.csv"
    ).exists():
        return "pipeline_e2e"
    return None


def main() -> None:
    moved = {"main": [], "aborts": [], "pipeline_e2e": [], "csv_sf_upload": []}

    # 1) Non-enrichment run dirs sitting at runs/ root (if any)
    for child in list(RUNS.iterdir()):
        if not child.is_dir():
            continue
        if child.name in {"enrichment", "_artifacts", "pipeline_e2e", "csv_sf_upload"}:
            continue
        if _is_enrichment_run(child):
            continue
        kind = _classify_pipeline_signals(child)
        if kind == "pipeline_e2e":
            _move(child, PIPELINE)
            moved["pipeline_e2e"].append(child.name)
        elif kind == "csv_sf_upload":
            _move(child, CSV_UPLOAD)
            moved["csv_sf_upload"].append(child.name)

    # 2) Collapse all enrichment runs into two process folders
    for run in _iter_enrichment_runs():
        n = _detail_n(run)
        target = THEME_ABORTS if n < 50 else THEME_MAIN
        # skip if already correctly placed
        if run.parent.resolve() == target.resolve():
            continue
        dest = _move(run, target)
        key = "aborts" if target == THEME_ABORTS else "main"
        moved[key].append(dest.name)
        print(f"MOVE {run.name} -> {target.relative_to(RUNS)}/  (n~{n})")

    # 3) Remove empty street-prefix / old theme dirs
    if ENRICH_ROOT.exists():
        for theme in list(ENRICH_ROOT.iterdir()):
            if not theme.is_dir():
                continue
            if theme.name in {THEME_MAIN.name, THEME_ABORTS.name}:
                continue
            # remove if empty of run dirs
            leftover = [p for p in theme.rglob("*_sf_enrichment") if p.is_dir()]
            if not leftover:
                shutil.rmtree(theme)
                print(f"REMOVED empty theme {theme.name}")
            else:
                print(f"WARN leftover runs in {theme}")

    # Ensure placeholders exist for other process families
    for path, blurb in (
        (PIPELINE, "Full orchestrator: CSV/source → dedupe → Nearmap/Claude classify → SF create"),
        (CSV_UPLOAD, "Direct sf_upload.csv → Site__c create (scripts/upload_sf_csv.py)"),
    ):
        path.mkdir(parents=True, exist_ok=True)
        readme = path / "README.md"
        if not readme.exists():
            readme.write_text(
                f"# `{path.name}`\n\n{blurb}\n\n"
                "No run folders found here yet — drop historical/future runs into this folder.\n",
                encoding="utf-8",
            )

    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    # 4) READMEs
    main_runs = sorted(p.name for p in THEME_MAIN.glob("*_sf_enrichment") if p.is_dir())
    abort_runs = sorted(p.name for p in THEME_ABORTS.glob("*_sf_enrichment") if p.is_dir())

    (ENRICH_ROOT / "README.md").write_text(
        "\n".join(
            [
                "# Enrichment runs (process theme)",
                "",
                "These are **Salesforce blank Site_Type__c enrichment** runs:",
                "query SF → FCC/TowerSource proximity → **NAIP-only** classify → SF update.",
                "",
                "This is **not** the full CSV→dedupe→Nearmap/Claude→create pipeline.",
                "Street-number query filters (`LIKE '1%'` …) were only batching within this",
                "same enrichment process — they are not separate themes.",
                "",
                f"## `sf_blank_site_type_naip_aug2026` ({len(main_runs)} runs)",
                "Substantive Aug 2026 enrichment campaign.",
                "",
                f"## `aborts_smoke` ({len(abort_runs)} runs)",
                "Empty or tiny launches (n < 50).",
                "",
                "Combined latest-per-Id CSV: `runs/_artifacts/_combined_enrichment_latest.csv`",
                "",
                "Other process families (siblings under `runs/`):",
                "- `pipeline_e2e/` — full orchestrator",
                "- `csv_sf_upload/` — upload template creates",
                "",
            ]
        ),
        encoding="utf-8",
    )

    index = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "note": "Organized by pipeline process, not street-prefix batch filters.",
        "themes": {
            "enrichment/sf_blank_site_type_naip_aug2026": {
                "process": "SF blank Site_Type → NAIP enrichment → SF update",
                "runs": len(main_runs),
                "items": main_runs,
            },
            "enrichment/aborts_smoke": {
                "process": "aborted/smoke enrichment launches",
                "runs": len(abort_runs),
                "items": abort_runs,
            },
            "pipeline_e2e": {
                "process": "CSV → dedupe → classify → SF create",
                "runs": len(list(PIPELINE.glob("*"))) - 1,  # minus README
            },
            "csv_sf_upload": {
                "process": "sf_upload.csv → Site__c create",
                "runs": len(list(CSV_UPLOAD.glob("*"))) - 1,
            },
        },
        "moved_this_pass": moved,
    }
    (ENRICH_ROOT / "README_themes.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    # drop obsolete street-prefix index naming
    old = ENRICH_ROOT / "README_themes.json"

    print(json.dumps({
        "main_runs": len(main_runs),
        "abort_runs": len(abort_runs),
        "pipeline_e2e_placeholder": str(PIPELINE),
        "csv_sf_upload_placeholder": str(CSV_UPLOAD),
    }, indent=2))


if __name__ == "__main__":
    main()
