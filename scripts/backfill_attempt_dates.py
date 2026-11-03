"""Backfill Site__c.LLM_Last_Attempted_Date__c from ICEMAN history.

python scripts/backfill_attempt_dates.py                 # plan only
python scripts/backfill_attempt_dates.py --apply         # write the plan

The last attempt per site is the newest of:

* a live run folder that processed it: its ``sf_update_apply_log.csv`` rows
  (any outcome) and ``enrichment_detail.csv`` rows, dated by the folder's
  start stamp (local time, converted to UTC). SQL-error and Nearmap-budget
  holdouts were not real attempts and are skipped, as live runs do now.
  A folder is live when its apply log has a non-dry-run row.
* Salesforce field history (exact time; covers writes with no saved log):
  Automation User setting ``LLM_Classified__c`` or ``LLM_Holdout__c`` to
  true, or writing ``Site_Type__c`` or an ICEMAN ``Verified_Site_Source__c``
  on or after ICEMAN's first run. The first runs (2026-07-30 to
  08-03) predate the saved run folders and LLM_Classified history tracking.

Nothing before ICEMAN's first run (2026-07-30) counts, from any source.
Sites with none of these stay blank (human-verified sites someone flagged by
hand). Only the attempt-date field is written, never over a newer value
already in Salesforce, so re-running is safe. Duplicate-rule matches are
saved anyway: the write touches one date field and creates nothing.
Writes ``<data>/backfill/attempt_dates_<stamp>/plan.csv`` and, with
``--apply``, ``apply_log.csv`` next to it.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from enrichment.sf_ops import (  # noqa: E402
    COLLECTION_LIMIT,
    attempted_field,
    query_all,
)
from paths import data_root, runs_dir  # noqa: E402

csv.field_size_limit(10**9)

_STAMP = re.compile(r"(20\d\d-\d\d-\d\d)(?:_(\d{6}))?")
_NOT_ATTEMPTED = {"sql_error", "nearmap_budget"}
_FLAG_FIELDS = ("LLM_Classified__c", "LLM_Holdout__c")
_HISTORY_FIELDS = (*_FLAG_FIELDS, "Site_Type__c", "Verified_Site_Source__c")
_ICEMAN_SOURCES = {"FCC", "TowerSource", "NAIP", "NearMap", "Google Map"}
ICEMAN_FIRST_RUN = "2026-07-30T00:00:00Z"
_ID_CHUNK = 300
_ALLOW_DUPLICATES = {"Sforce-Duplicate-Rule-Header": "allowSave=true"}


def folder_time(path: Path) -> str | None:
    """UTC ISO time from the nearest run-folder stamp (local time), else None."""
    for part in reversed(path.parent.parts):
        match = _STAMP.search(part)
        if not match:
            continue
        day, hms = match.group(1), match.group(2) or "120000"
        local = datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H%M%S").astimezone()
        return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        yield from csv.DictReader(handle)


def _keep_latest(best: dict[str, tuple[str, str]], sf_id: str, when: str, source: str) -> None:
    sf_id = (sf_id or "").strip()
    if len(sf_id) < 15 or not when or when < ICEMAN_FIRST_RUN:
        return
    key = sf_id[:15]
    if key not in best or when > best[key][0]:
        best[key] = (when, source)


def history_from_runs(root: Path) -> tuple[dict[str, tuple[str, str]], int]:
    """Newest live-run time per 15-char Id, and how many live folders were read."""
    best: dict[str, tuple[str, str]] = {}
    live = 0
    for log in sorted(root.rglob("sf_update_apply_log.csv")):
        rows = [r for r in _rows(log) if str(r.get("dry_run")).strip().lower() == "false"]
        when = folder_time(log)
        if not rows or not when:
            continue
        live += 1
        source = f"run:{log.parent.relative_to(root)}"
        for row in rows:
            _keep_latest(best, row.get("Id", ""), when, source)
        for detail in log.parent.glob("*enrichment_detail.csv"):
            for row in _rows(detail):
                if str(row.get("holdout_reason") or "").strip() in _NOT_ATTEMPTED:
                    continue
                _keep_latest(best, row.get("Id", ""), when, source)
    return best, live


def _automation_user_id(sf) -> str:
    me = sf.restful("chatter/users/me")
    return str(me["id"])


def _iceman_write(row: dict) -> bool:
    """True when an Automation User history row is an ICEMAN decision write."""
    field, value = row.get("Field"), row.get("NewValue")
    if field in _FLAG_FIELDS:
        return value is True
    if field == "Verified_Site_Source__c":
        return value in _ICEMAN_SOURCES
    return bool(value)  # Site_Type__c set to anything


def history_from_salesforce(client) -> dict[str, tuple[str, str]]:
    """Newest Automation User ICEMAN write, per 15-char Id."""
    user_id = _automation_user_id(client.sf)
    fields = ", ".join(f"'{f}'" for f in _HISTORY_FIELDS)
    soql = (
        "SELECT ParentId, Field, NewValue, CreatedDate FROM Site__History "
        f"WHERE Field IN ({fields}) AND CreatedById = '{user_id}' "
        f"AND CreatedDate >= {ICEMAN_FIRST_RUN}"
    )
    best: dict[str, tuple[str, str]] = {}
    for row in query_all(client, soql):
        if not _iceman_write(row):
            continue
        when = datetime.strptime(row["CreatedDate"][:19], "%Y-%m-%dT%H:%M:%S")
        _keep_latest(
            best,
            row["ParentId"],
            when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"sf_history:{row['Field']}",
        )
    return best


def _send(client, updates: list[tuple[str, dict[str, str]]]) -> list[str | None]:
    """sObject Collections PATCH that saves through duplicate-rule matches."""
    body = {
        "allOrNone": False,
        "records": [{"attributes": {"type": "Site__c"}, "id": i, **p} for i, p in updates],
    }
    reply = client.sf.restful(
        "composite/sobjects", method="PATCH", json=body, headers=_ALLOW_DUPLICATES
    )
    return [
        None if item.get("success")
        else "; ".join(f"{e.get('statusCode')}: {e.get('message')}" for e in item.get("errors") or [])
        for item in reply
    ]


def current_values(client, ids: list[str], field: str) -> dict[str, tuple[str, str]]:
    """15-char Id → (18-char Id, current field value) for sites that still exist."""
    out: dict[str, tuple[str, str]] = {}
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ", ".join(f"'{i}'" for i in ids[start : start + _ID_CHUNK])
        for row in query_all(client, f"SELECT Id, {field} FROM Site__c WHERE Id IN ({chunk})"):
            value = str(row.get(field) or "")
            out[row["Id"][:15]] = (row["Id"], value[:19] + "Z" if value else "")
    return out


def build_plan(client, field: str, root: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    runs, live = history_from_runs(root)
    hist = history_from_salesforce(client)
    merged = dict(runs)
    for key, (when, source) in hist.items():
        _keep_latest(merged, key, when, source)
    existing = current_values(client, sorted(merged), field)
    plan: list[dict[str, str]] = []
    stats = {
        "live_run_folders": live,
        "ids_from_runs": len(runs),
        "ids_from_sf_history": len(hist),
        "ids_dated": len(merged),
        "missing_in_salesforce": 0,
        "already_current": 0,
    }
    for key, (when, source) in sorted(merged.items()):
        if key not in existing:
            stats["missing_in_salesforce"] += 1
            continue
        sf_id, current = existing[key]
        if current and current >= when:
            stats["already_current"] += 1
            continue
        plan.append({"Id": sf_id, field: when, "source": source, "current": current})
    stats["to_write"] = len(plan)
    return plan, stats


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def apply_plan(client, plan: list[dict[str, str]], field: str, log_path: Path) -> dict[str, int]:
    done = {"updated": 0, "failed": 0}
    with log_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Id", field, "success", "error"])
        writer.writeheader()
        for start in range(0, len(plan), COLLECTION_LIMIT):
            chunk = plan[start : start + COLLECTION_LIMIT]
            try:
                errors = _send(client, [(row["Id"], {field: row[field]}) for row in chunk])
            except Exception as exc:  # noqa: BLE001 — record and keep going
                errors = [str(exc)] * len(chunk)
            for row, error in zip(chunk, errors):
                done["failed" if error else "updated"] += 1
                writer.writerow(
                    {"Id": row["Id"], field: row[field], "success": not error, "error": error or ""}
                )
            handle.flush()
            print(f"  {start + len(chunk):,}/{len(plan):,} sent | "
                  f"updated={done['updated']:,} failed={done['failed']:,}", flush=True)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the plan to Salesforce")
    parser.add_argument("--runs", type=Path, default=None, help="run folders root")
    parser.add_argument("--limit", type=int, default=None, help="apply at most N rows")
    args = parser.parse_args(argv)

    field = attempted_field()
    if not field:
        raise SystemExit("LLM_ATTEMPTED_FIELD is off; nothing to backfill")
    from salesforce.sf_client import SalesforceClient

    client = SalesforceClient()
    plan, stats = build_plan(client, field, args.runs or runs_dir())
    out_dir = data_root() / "backfill" / f"attempt_dates_{datetime.now():%Y-%m-%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "plan.csv", plan, ["Id", field, "source", "current"])
    for key, value in stats.items():
        print(f"  {key:24s} {value:,}")
    by_month: dict[str, int] = {}
    for row in plan:
        by_month[row[field][:7]] = by_month.get(row[field][:7], 0) + 1
    print("  to_write by month:", dict(sorted(by_month.items())))
    print(f"  plan -> {out_dir / 'plan.csv'}")
    if not args.apply:
        print("  plan only; rerun with --apply to write")
        return 0
    todo = plan[: args.limit] if args.limit else plan
    done = apply_plan(client, todo, field, out_dir / "apply_log.csv")
    print(f"  applied: {done} | log -> {out_dir / 'apply_log.csv'}")
    return 0 if not done["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
