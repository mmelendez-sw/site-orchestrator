"""Restore Unqualified LLM-no-asset sites back into the enrichment queue.

Reads a candidate CSV (Id column), optionally filters to apply-log successes,
and sets Stage + LLM flags so ``python -m enrichment --ids ...`` can re-classify.

Default restore payload:
  Stage__c = Enhanced/Unreviewed
  LLM_Classified__c = true
  LLM_Holdout__c = false
  Unqualified_Reason__c = null
  Site_Type__c left unchanged (should already be blank for this cohort)

Live SF owner/stage guards mirror the unqualified script (skip protected stages
and non Site-Acquisition / Marketing owners).

Dry-run by default. ``--apply`` writes Salesforce.

Examples:
  python scripts/restore_unqualified_for_rerun.py ^
    --candidates runs/_rerun_proximity_fn_candidates.csv

  python scripts/restore_unqualified_for_rerun.py ^
    --candidates runs/_rerun_proximity_fn_candidates.csv ^
    --apply-log runs/_no_asset_unqualified_2026-08-26_104537_apply_log.csv ^
    --apply -v
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from enrichment.sf_ops import _soql_in, query_all, update_site  # noqa: E402
from salesforce.field_map import OBJECT_NAME  # noqa: E402
from salesforce.sf_client import SalesforceClient  # noqa: E402

logger = logging.getLogger(__name__)

SITE_ACQUISITION_OWNER_ID = "0053l00000G05h9AAB"
ALLOWED_OWNER_TEXT_NEEDLES = (
    "marketing campaign",
    "site acquisition",
)
PROTECTED_STAGES = frozenset(
    {
        "Qualified (Converted)",
        "Outreach - Verified",
        "Working - Connected",
    }
)
DEFAULT_STAGE = "Enhanced/Unreviewed"


def load_ids_from_csv(path: Path) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            sid = str(row.get("Id") or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                ids.append(sid)
    return ids


def load_apply_success_ids(path: Path) -> set[str]:
    ok: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("status") or "").strip().lower() == "ok":
                sid = str(row.get("Id") or "").strip()
                if sid:
                    ok.add(sid)
    return ok


def query_live(
    client: SalesforceClient,
    ids: Sequence[str],
    *,
    chunk_size: int = 200,
) -> dict[str, dict[str, Any]]:
    clean = [str(i).strip() for i in ids if str(i).strip()]
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(clean), chunk_size):
        chunk = clean[start : start + chunk_size]
        soql = (
            f"SELECT Id, OwnerId, Owner__c, Stage__c, Site_Type__c, "
            f"LLM_Classified__c, LLM_Holdout__c, Unqualified_Reason__c "
            f"FROM {OBJECT_NAME} WHERE Id IN ({_soql_in(chunk)})"
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


def skip_reason(live: dict[str, Any] | None) -> str | None:
    if live is None:
        return "not_found_in_salesforce"
    stage = str(live.get("Stage__c") or "").strip()
    if stage in PROTECTED_STAGES:
        return f"protected_stage:{stage}"
    if not is_owner_allowed(live):
        return f"owner_not_allowed:{live.get('OwnerId')}|{live.get('Owner__c')}"
    return None


def restore_payload(*, stage: str) -> dict[str, Any]:
    return {
        "Stage__c": stage,
        "LLM_Classified__c": True,
        "LLM_Holdout__c": False,
        "Unqualified_Reason__c": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore Unqualified no-asset sites for enrichment re-run."
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        required=True,
        help="CSV with Id column (e.g. proximity FN candidates)",
    )
    parser.add_argument(
        "--apply-log",
        type=Path,
        default=None,
        help="Optional unqualified apply_log.csv — keep only status=ok Ids",
    )
    parser.add_argument(
        "--stage",
        default=DEFAULT_STAGE,
        help=f"Stage to restore (default: {DEFAULT_STAGE})",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    ids = load_ids_from_csv(args.candidates)
    if args.apply_log:
        if not args.apply_log.exists():
            logging.error("Apply log not found yet: %s", args.apply_log)
            return 1
        ok_ids = load_apply_success_ids(args.apply_log)
        before = len(ids)
        ids = [i for i in ids if i in ok_ids]
        print(f"Apply-log filter: {before} -> {len(ids)} (status=ok)", flush=True)

    if args.limit is not None:
        ids = ids[: max(0, args.limit)]

    print("=== AUTHENTICATE SALESFORCE ===", flush=True)
    client = SalesforceClient()
    print("  authenticated", flush=True)

    live_map = query_live(client, ids)
    eligible: list[str] = []
    skipped: list[tuple[str, str]] = []
    for sid in ids:
        skip = skip_reason(live_map.get(sid))
        if skip:
            skipped.append((sid, skip))
        else:
            eligible.append(sid)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = args.out or (
        ROOT / "runs" / f"_restore_for_rerun_{stamp}.csv"
    )
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Id",
                "eligibility",
                "sf_Stage__c",
                "sf_OwnerId",
                "sf_Owner__c",
                "sf_Site_Type__c",
                "sf_Unqualified_Reason__c",
            ],
        )
        writer.writeheader()
        for sid in eligible:
            live = live_map.get(sid) or {}
            writer.writerow(
                {
                    "Id": sid,
                    "eligibility": "eligible",
                    "sf_Stage__c": live.get("Stage__c") or "",
                    "sf_OwnerId": live.get("OwnerId") or "",
                    "sf_Owner__c": live.get("Owner__c") or "",
                    "sf_Site_Type__c": live.get("Site_Type__c") or "",
                    "sf_Unqualified_Reason__c": live.get("Unqualified_Reason__c") or "",
                }
            )
        for sid, reason in skipped:
            live = live_map.get(sid) or {}
            writer.writerow(
                {
                    "Id": sid,
                    "eligibility": f"skip:{reason}",
                    "sf_Stage__c": live.get("Stage__c") or "",
                    "sf_OwnerId": live.get("OwnerId") or "",
                    "sf_Owner__c": live.get("Owner__c") or "",
                    "sf_Site_Type__c": live.get("Site_Type__c") or "",
                    "sf_Unqualified_Reason__c": live.get("Unqualified_Reason__c") or "",
                }
            )

    print(
        f"candidates={len(ids)} eligible={len(eligible)} skipped={len(skipped)} | "
        f"{'LIVE APPLY' if args.apply else 'DRY-RUN'} | stage={args.stage!r}",
        flush=True,
    )
    print(f"Preview -> {out_path.resolve()}", flush=True)
    if eligible:
        id_arg = ",".join(eligible)
        print(
            "Re-enrich command after restore:\n"
            f"  python -m enrichment --ids {id_arg[:120]}"
            f"{'...' if len(id_arg) > 120 else ''} -v\n"
            "(use the Ids file / full comma list; omit --apply until review)",
            flush=True,
        )

    if not args.apply:
        print("Dry-run only. Re-run with --apply to restore Salesforce.", flush=True)
        return 0

    if not eligible:
        print("Nothing to restore.", flush=True)
        return 0

    payload = restore_payload(stage=args.stage)
    print(f"Payload: {payload}", flush=True)
    ok = 0
    failed = 0
    log_path = out_path.with_name(out_path.stem + "_apply_log.csv")
    log_rows: list[dict[str, Any]] = []
    for index, sid in enumerate(eligible, start=1):
        try:
            update_site(client, sid, payload, verbose=args.verbose)
            ok += 1
            status = "ok"
            err = ""
            if args.verbose or index % 50 == 0 or index == len(eligible):
                print(f"[{index}/{len(eligible)}] OK {sid}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            status = "error"
            err = str(exc)
            print(f"[{index}/{len(eligible)}] FAIL {sid} | {err}", flush=True)
        log_rows.append(
            {"index": index, "Id": sid, "status": status, "error": err, "payload": str(payload)}
        )

    with log_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["index", "Id", "status", "error", "payload"]
        )
        writer.writeheader()
        writer.writerows(log_rows)

    print(f"Done. success={ok} failed={failed} log->{log_path.resolve()}", flush=True)

    ids_path = out_path.with_name(out_path.stem + "_ids.txt")
    ids_path.write_text(",".join(eligible), encoding="utf-8")
    print(f"Ids for enrichment --ids @file / paste: {ids_path.resolve()}", flush=True)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
