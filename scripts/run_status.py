"""Live status of ICEMAN runs (read-only): verdicts, Salesforce writes, Nearmap MB.

  python scripts/run_status.py g1ov g2free            # one snapshot of those run names
  python scripts/run_status.py g1ov g2free --watch 15 # refresh every 15 s (Ctrl+C to stop)

Each name matches run folders ``runs/<date>_<time>_<name>_L*`` from today
(``--date`` to change). Rows appear as each site finishes, before the batch
writes to Salesforce (shown as ``pending`` until then).
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rows(runs: Path, day: str, name: str) -> list[dict]:
    rows: list[dict] = []
    for path in glob.glob(str(runs / f"{day}_*_{name}_L*" / "enrichment_detail.csv")):
        try:
            with open(path, newline="", encoding="utf-8-sig") as handle:
                rows.extend(csv.DictReader(handle))
        except OSError:
            continue
    return rows


def _lanes(runs: Path, day: str, name: str) -> str:
    folders = sorted(glob.glob(str(runs / f"{day}_*_lanes_{name}")))
    if not folders:
        return "not started"
    events = Path(folders[-1]) / "events.log"
    try:
        lines = events.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "starting"
    done = sum(" done (exit" in line for line in lines)
    failed = sum(" failed (exit" in line for line in lines)
    running = sum(" start (" in line for line in lines) - done - failed
    return f"batches done {done}, running {max(0, running)}, failed {failed}"


def snapshot(names: list[str], day: str) -> str:
    from paths import runs_dir

    runs = runs_dir()
    out = [f"ICEMAN status {time.strftime('%H:%M:%S')}"]
    for name in names:
        rows = _rows(runs, day, name)
        verdicts = Counter(r.get("audit_verdict") or r.get("bucket") or "?" for r in rows)
        sf = Counter(r.get("sf_update_status") or "?" for r in rows)
        mb = sum(float(r.get("nearmap_bytes") or 0) for r in rows) / 1_048_576
        types = Counter(r.get("update_site_type") for r in rows if r.get("audit_verdict") == "confirmed")
        out.append(f"\n[{name}] {_lanes(runs, day, name)}")
        out.append(f"  sites done   {len(rows)}")
        out.append("  verdicts     " + ", ".join(f"{k} {v}" for k, v in verdicts.most_common()))
        if types:
            out.append("  confirmed as " + ", ".join(f"{k} {v}" for k, v in types.most_common()))
        out.append("  salesforce   " + ", ".join(f"{k} {v}" for k, v in sf.most_common()))
        out.append(f"  nearmap      {mb:.1f} MB")
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        from enrichment.lanes import month_to_date_mb

        out.append(f"\nNearmap month-to-date (ICEMAN ledger): {month_to_date_mb():,.0f} MB")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("names", nargs="+", help="run names given to enrichment.lanes --run-name")
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--watch", type=float, default=0, help="refresh every N seconds")
    args = p.parse_args(argv)
    while True:
        text = snapshot(args.names, args.date)
        if args.watch:
            os.system("cls" if os.name == "nt" else "clear")
        print(text, flush=True)
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
