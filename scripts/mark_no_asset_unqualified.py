"""Mark clear no-asset enrichment results as LLM_Classified=true + Unqualified.

By default reads ``runs/_combined_enrichment_latest.csv`` (~57k sites).

**Tiers:** ``other`` (strong, default) vs ``unclear`` (noisy low-conf).

**Unqualified Reason (required by SF validation):**
  - ``other`` → ``Not a Cellular tower``
  - otherwise → ``No Site/Decommissioned``
  Override with ``--unqualified-reason``.

**Live SF eligibility (checked in realtime before write):**
  - OwnerId = Site Acquisition team ``0053l00000G05h9AAB``
    OR Owner__c contains ``Marketing Campaign`` / ``Site Acquisition``
  - Stage__c NOT IN protected stages:
    Qualified (Converted), Outreach - Verified, Working - Connected

Dry-run by default. Live writes require ``--apply``.

  python scripts/mark_no_asset_unqualified.py --tier other --min-site-conf 0.75 --carrier-like none --apply
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from enrichment.constants import BUCKET_POTENTIAL_UPDATE  # noqa: E402
from enrichment.sf_ops import _soql_in, query_all, update_site  # noqa: E402
from salesforce.field_map import OBJECT_NAME  # noqa: E402
from salesforce.sf_client import SalesforceClient  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_FROM = date(2026, 8, 4)
DEFAULT_TO = date(2026, 8, 11)
DEFAULT_STAGE = "Unqualified"
DEFAULT_COMBINED = ROOT / "runs" / "_combined_enrichment_latest.csv"
DEFAULT_CARRIER_LIKE = "MM_NFLPermittingSites,MM_PermittingSites"
DEFAULT_MIN_SITE_CONF = 0.75

# Standard OwnerId for Site Acquisition Team (from ops).
SITE_ACQUISITION_OWNER_ID = "0053l00000G05h9AAB"
ALLOWED_OWNER_TEXT_NEEDLES = (
    "marketing campaign",
    "site acquisition",
)

# Exact SF Stage__c picklist values that must never be overwritten.
PROTECTED_STAGES = frozenset(
    {
        "Qualified (Converted)",
        "Outreach - Verified",
        "Working - Connected",
    }
)

REASON_NOT_CELLULAR = "Not a Cellular tower"
REASON_NO_SITE = "No Site/Decommissioned"

NO_CELL_EXTRA_REASONS = frozenset(
    {
        "rooftop_no_cell_equipment",
        "tower_no_cell_equipment",
        "unmapped_site_type",
    }
)
EXCLUDE_REASONS = frozenset(
    {
        "classify_error",
        "sql_error",
        "missing_sf_coordinates",
        "missing_coordinates",
        "skip_classify_db_hit",
        "skip_classify_no_db_hit",
        "no_naip_imagery",
        "no_imagery",
        "potential_rooftop",
    }
)


def _parse_run_date(name: str) -> date | None:
    try:
        return datetime.strptime(str(name)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _site_confidence(row: dict[str, Any]) -> float | None:
    raw = row.get("naip_site_confidence")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _row_label(row: dict[str, Any]) -> str:
    reason = str(row.get("holdout_reason") or "").strip().lower()
    site_type = str(row.get("naip_site_type") or row.get("site_type") or "").strip().lower()
    if reason in {"other", "unclear"}:
        return reason
    if site_type in {"other", "unclear"}:
        return site_type
    if reason in NO_CELL_EXTRA_REASONS:
        return reason
    return reason or site_type or "?"


def choose_unqualified_reason(row: dict[str, Any], override: str | None = None) -> str:
    if override:
        return override
    label = _row_label(row)
    if label == "other":
        return REASON_NOT_CELLULAR
    return REASON_NO_SITE


def iter_run_dirs(
    runs_root: Path,
    *,
    from_date: date,
    to_date: date,
) -> list[Path]:
    dirs: list[Path] = []
    for path in sorted(runs_root.glob("*_sf_enrichment")):
        if not path.is_dir():
            continue
        run_day = _parse_run_date(path.name)
        if run_day is None:
            continue
        if from_date <= run_day <= to_date:
            dirs.append(path)
    return dirs


def load_latest_rows_by_id(run_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for run_dir in run_dirs:
        detail = run_dir / "enrichment_detail.csv"
        if not detail.exists():
            continue
        with detail.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sid = str(row.get("Id") or "").strip()
                if not sid:
                    continue
                out = dict(row)
                out["_run_dir"] = run_dir.name
                by_id[sid] = out
    return by_id


def load_combined_latest_by_id(
    combined_csv: Path,
    *,
    from_date: date,
    to_date: date,
) -> dict[str, dict[str, Any]]:
    if not combined_csv.exists():
        raise FileNotFoundError(f"Combined CSV not found: {combined_csv}")

    scored: dict[str, tuple[str, dict[str, Any]]] = {}
    with combined_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            sid = str(row.get("Id") or "").strip()
            if not sid:
                continue
            run_name = str(row.get("_run") or row.get("run_dir") or "").strip()
            if not run_name:
                continue
            run_day = _parse_run_date(run_name)
            if run_day is None or not (from_date <= run_day <= to_date):
                continue
            out = dict(row)
            out["_run_dir"] = run_name
            prev = scored.get(sid)
            if prev is None or run_name >= prev[0]:
                scored[sid] = (run_name, out)
    return {sid: pair[1] for sid, pair in scored.items()}


def is_distinct_no_asset(
    row: dict[str, Any],
    *,
    tier: str,
    mode: str,
    min_site_conf: float | None,
) -> bool:
    bucket = str(row.get("bucket") or "").strip()
    if bucket == BUCKET_POTENTIAL_UPDATE:
        return False

    reason = str(row.get("holdout_reason") or "").strip()
    if reason in EXCLUDE_REASONS or reason.startswith("asset_offset"):
        return False

    label = _row_label(row)
    allowed_labels: set[str] = set()
    if tier in {"other", "both"}:
        allowed_labels.add("other")
    if tier in {"unclear", "both"}:
        allowed_labels.add("unclear")
    if mode == "no_cell":
        allowed_labels |= NO_CELL_EXTRA_REASONS

    if label not in allowed_labels and not (
        reason.startswith("else:") and tier in {"other", "both"}
    ):
        return False

    if min_site_conf is not None:
        conf = _site_confidence(row)
        if conf is None or conf < min_site_conf:
            return False
    return True


def query_ids_matching_carrier_like(
    client: SalesforceClient,
    patterns: Sequence[str],
) -> dict[str, str]:
    clean = [p.strip() for p in patterns if str(p).strip()]
    if not clean:
        return {}
    likes = " OR ".join(
        "Carrier_Leasing_Source__c LIKE '%"
        + p.replace("\\", "\\\\").replace("'", "\\'")
        + "%'"
        for p in clean
    )
    soql = (
        f"SELECT Id, Carrier_Leasing_Source__c FROM {OBJECT_NAME} "
        f"WHERE {likes}"
    )
    logger.info("Salesforce SOQL: %s", soql)
    rows = query_all(client, soql)
    out: dict[str, str] = {}
    for row in rows:
        sid = str(row.get("Id") or "").strip()
        if sid:
            out[sid] = str(row.get("Carrier_Leasing_Source__c") or "")
    return out


def query_live_site_guards(
    client: SalesforceClient,
    ids: Sequence[str],
    *,
    chunk_size: int = 200,
) -> dict[str, dict[str, Any]]:
    """Fetch current OwnerId / Owner__c / Stage__c for eligibility checks."""
    clean = [str(i).strip() for i in ids if str(i).strip()]
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(clean), chunk_size):
        chunk = clean[start : start + chunk_size]
        soql = (
            f"SELECT Id, OwnerId, Owner__c, Stage__c FROM {OBJECT_NAME} "
            f"WHERE Id IN ({_soql_in(chunk)})"
        )
        for row in query_all(client, soql):
            sid = str(row.get("Id") or "").strip()
            if sid:
                out[sid] = row
    return out


def is_owner_allowed(live: dict[str, Any]) -> bool:
    owner_id = str(live.get("OwnerId") or "").strip()
    if owner_id == SITE_ACQUISITION_OWNER_ID:
        return True
    owner_text = str(live.get("Owner__c") or "").strip().lower()
    return any(needle in owner_text for needle in ALLOWED_OWNER_TEXT_NEEDLES)


def eligibility_skip_reason(live: dict[str, Any] | None) -> str | None:
    if live is None:
        return "not_found_in_salesforce"
    stage = str(live.get("Stage__c") or "").strip()
    if stage in PROTECTED_STAGES:
        return f"protected_stage:{stage}"
    if not is_owner_allowed(live):
        return (
            f"owner_not_allowed:OwnerId={live.get('OwnerId') or ''}|"
            f"Owner__c={live.get('Owner__c') or ''}"
        )
    return None


def build_payload(
    *,
    stage: str,
    clear_holdout: bool,
    unqualified_reason: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "LLM_Classified__c": True,
        "Stage__c": stage,
        "Unqualified_Reason__c": unqualified_reason,
    }
    if clear_holdout:
        payload["LLM_Holdout__c"] = False
    return payload


def write_preview_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "Id",
        "_run_dir",
        "bucket",
        "holdout_reason",
        "naip_site_type",
        "naip_site_confidence",
        "naip_cell_equipment",
        "unqualified_reason",
        "Carrier_Leasing_Source__c",
        "sf_OwnerId",
        "sf_Owner__c",
        "sf_Stage__c",
        "eligibility",
        "Site_Street__c",
        "Site_City__c",
        "Site_State__c",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Mark high-confidence no-asset enrichment Ids as "
            "LLM_Classified=true + Stage=Unqualified + Unqualified_Reason__c, "
            "only when live SF owner/stage checks pass."
        )
    )
    parser.add_argument("--source", choices=("combined", "runs"), default="combined")
    parser.add_argument("--combined", type=Path, default=DEFAULT_COMBINED)
    parser.add_argument("--runs-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--from-date", type=str, default=DEFAULT_FROM.isoformat())
    parser.add_argument("--to-date", type=str, default=DEFAULT_TO.isoformat())
    parser.add_argument(
        "--tier",
        choices=("other", "unclear", "both"),
        default="other",
    )
    parser.add_argument(
        "--mode",
        choices=("no_asset", "no_cell"),
        default="no_asset",
    )
    parser.add_argument(
        "--min-site-conf",
        type=float,
        default=DEFAULT_MIN_SITE_CONF,
    )
    parser.add_argument(
        "--carrier-like",
        type=str,
        default=DEFAULT_CARRIER_LIKE,
        help="Comma LIKE needles, or 'none' to skip carrier filter.",
    )
    parser.add_argument("--stage", default=DEFAULT_STAGE)
    parser.add_argument(
        "--unqualified-reason",
        type=str,
        default=None,
        help=(
            f"Force Unqualified_Reason__c. Default auto: other→{REASON_NOT_CELLULAR!r}, "
            f"else→{REASON_NO_SITE!r}"
        ),
    )
    parser.add_argument("--keep-holdout", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ids", type=str, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)
    if to_date < from_date:
        logging.error("--to-date must be >= --from-date")
        return 1

    min_conf: float | None = float(args.min_site_conf)
    if min_conf <= 0:
        min_conf = None

    if args.source == "combined":
        latest = load_combined_latest_by_id(
            args.combined, from_date=from_date, to_date=to_date
        )
        source_label = str(args.combined)
    else:
        run_dirs = iter_run_dirs(
            args.runs_root, from_date=from_date, to_date=to_date
        )
        if not run_dirs:
            logging.error("No enrichment runs found in %s .. %s", from_date, to_date)
            return 1
        latest = load_latest_rows_by_id(run_dirs)
        source_label = f"{len(run_dirs)} on-disk run dirs"

    allow: set[str] | None = None
    if args.ids:
        allow = {p.strip() for p in args.ids.split(",") if p.strip()}

    selected = [
        row
        for sid, row in sorted(latest.items())
        if (allow is None or sid in allow)
        and is_distinct_no_asset(
            row,
            tier=args.tier,
            mode=args.mode,
            min_site_conf=min_conf,
        )
    ]

    carrier_raw = str(args.carrier_like or "").strip()
    if carrier_raw.lower() in {"", "none", "-", "all", "*"}:
        carrier_patterns: list[str] = []
    else:
        carrier_patterns = [p.strip() for p in carrier_raw.split(",") if p.strip()]

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    client = SalesforceClient()
    print("  authenticated", flush=True)

    if carrier_patterns:
        print(f"Resolving carrier filter: {carrier_patterns}", flush=True)
        carrier_map = query_ids_matching_carrier_like(client, carrier_patterns)
        print(f"  SF carrier matches: {len(carrier_map)}", flush=True)
        filtered: list[dict[str, Any]] = []
        for row in selected:
            sid = str(row.get("Id") or "").strip()
            if sid in carrier_map:
                row = dict(row)
                row["Carrier_Leasing_Source__c"] = carrier_map[sid]
                filtered.append(row)
        print(f"  After carrier intersect: {len(selected)} -> {len(filtered)}", flush=True)
        selected = filtered

    if args.limit is not None:
        selected = selected[: max(0, args.limit)]

    # Live owner/stage eligibility.
    print(f"Fetching live SF owner/stage for {len(selected)} candidate(s)...", flush=True)
    live_map = query_live_site_guards(
        client, [str(r.get("Id") or "") for r in selected]
    )
    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    skip_counts: dict[str, int] = {}
    for row in selected:
        sid = str(row.get("Id") or "").strip()
        live = live_map.get(sid)
        out = dict(row)
        out["unqualified_reason"] = choose_unqualified_reason(
            row, args.unqualified_reason
        )
        out["sf_OwnerId"] = (live or {}).get("OwnerId") or ""
        out["sf_Owner__c"] = (live or {}).get("Owner__c") or ""
        out["sf_Stage__c"] = (live or {}).get("Stage__c") or ""
        skip = eligibility_skip_reason(live)
        if skip:
            out["eligibility"] = f"skip:{skip}"
            skipped.append(out)
            key = skip.split(":", 1)[0]
            skip_counts[key] = skip_counts.get(key, 0) + 1
        else:
            out["eligibility"] = "eligible"
            eligible.append(out)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = args.out or (args.runs_root / f"_no_asset_unqualified_{stamp}.csv")
    write_preview_csv(out_path, eligible + skipped)

    reason_counts: dict[str, int] = {}
    for row in eligible:
        key = row.get("unqualified_reason") or "?"
        reason_counts[str(key)] = reason_counts.get(str(key), 0) + 1

    print(
        f"SOURCE {source_label} | {from_date}..{to_date} | "
        f"candidates={len(selected)} | eligible={len(eligible)} | "
        f"skipped={len(skipped)} | tier={args.tier} | min_conf={min_conf} | "
        f"carrier={carrier_patterns or 'ALL'} | "
        f"{'LIVE APPLY' if args.apply else 'DRY-RUN'}",
        flush=True,
    )
    print("Eligible unqualified reasons:", flush=True)
    for reason, count in sorted(reason_counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {count:5d}  {reason}", flush=True)
    if skip_counts:
        print("Skipped:", flush=True)
        for key, count in sorted(skip_counts.items(), key=lambda x: (-x[1], x[0])):
            print(f"  {count:5d}  {key}", flush=True)
    print(f"Preview CSV -> {out_path.resolve()}", flush=True)

    if not eligible:
        print("Nothing eligible to update.", flush=True)
        return 0

    if not args.apply:
        print("Dry-run only. Re-run with --apply to write Salesforce.", flush=True)
        return 0

    ok = 0
    failed = 0
    log_path = out_path.with_name(out_path.stem + "_apply_log.csv")
    log_rows: list[dict[str, Any]] = []
    for index, row in enumerate(eligible, start=1):
        sid = str(row.get("Id") or "").strip()
        label = (
            f"{row.get('Site_Street__c') or ''}, {row.get('Site_City__c') or ''}"
        ).strip(", ")
        payload = build_payload(
            stage=args.stage,
            clear_holdout=not args.keep_holdout,
            unqualified_reason=str(row.get("unqualified_reason") or REASON_NOT_CELLULAR),
        )
        try:
            update_site(client, sid, payload, verbose=args.verbose)
            ok += 1
            status = "ok"
            err = ""
            if args.verbose or index % 100 == 0 or index == len(eligible):
                print(
                    f"[{index}/{len(eligible)}] OK {sid} | "
                    f"{payload['Unqualified_Reason__c']} | {label}",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            status = "error"
            err = str(exc)
            print(f"[{index}/{len(eligible)}] FAIL {sid} | {err}", flush=True)
        log_rows.append(
            {
                "index": index,
                "Id": sid,
                "status": status,
                "error": err,
                "unqualified_reason": row.get("unqualified_reason") or "",
                "sf_Stage__c": row.get("sf_Stage__c") or "",
                "sf_OwnerId": row.get("sf_OwnerId") or "",
                "payload": str(payload),
            }
        )

    with log_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "index",
                "Id",
                "status",
                "error",
                "unqualified_reason",
                "sf_Stage__c",
                "sf_OwnerId",
                "payload",
            ],
        )
        writer.writeheader()
        writer.writerows(log_rows)

    print(
        f"Done. success={ok} failed_or_skipped={failed} log->{log_path.resolve()}",
        flush=True,
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
