"""Load FCC ULS microwave license locations into dbo.FccUlsMicrowaveLocation.

Source: the ULS complete weekly microwave file (l_micro.zip, ~210 MB) from
https://data.fcc.gov/download/pub/uls/complete/l_micro.zip. HD.dat supplies
license status / radio service; LO.dat supplies locations (D/M/S + direction,
converted to signed decimal). Only active licenses (HD license_status 'A')
with valid coordinates are kept. Field positions: enrichment/signals/uls.py.

Uses the same Entra token connection as FCC/TowerSource (az login).

  python scripts/load_fcc_uls_microwave.py --dry-run                 # download + parse, print counts
  python scripts/load_fcc_uls_microwave.py --zip C:/path/l_micro.zip --dry-run
  python scripts/load_fcc_uls_microwave.py --zip C:/path/l_micro.zip  # load SQL

Load: run sql/fcc_uls_microwave.sql batches (create target + index, fresh
staging table), executemany into staging in batches, then replace the target
contents from staging in one transaction and drop staging.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from enrichment.signals.uls import LOCATION_COLUMNS, ULS_TABLE, build_locations  # noqa: E402
from envutil import env_str  # noqa: E402
from paths import data_root  # noqa: E402

FCC_ULS_MICRO_URL = env_str(
    "FCC_ULS_MICRO_URL", "https://data.fcc.gov/download/pub/uls/complete/l_micro.zip"
)
DDL_PATH = ROOT / "sql" / "fcc_uls_microwave.sql"
STAGING_TABLE = "dbo.FccUlsMicrowaveLocation_Staging"
INSERT_COLUMNS = LOCATION_COLUMNS


def default_zip_path() -> Path:
    return data_root() / "cache" / "fcc_uls" / "l_micro.zip"


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    print(f"downloading {url} -> {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": "site-orchestrator/uls-loader"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        total = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
    tmp.replace(dest)
    print(f"downloaded {total / 1e6:.1f} MB")
    return dest


def _member(zf: zipfile.ZipFile, name: str) -> str:
    for info in zf.infolist():
        if info.filename.split("/")[-1].upper() == name.upper():
            return info.filename
    raise SystemExit(f"{name} not found in zip")


def parse_zip(path: Path) -> tuple[list[dict], dict[str, int]]:
    with zipfile.ZipFile(path) as zf:
        hd_name, lo_name = _member(zf, "HD.dat"), _member(zf, "LO.dat")
        with zf.open(hd_name) as hd_raw, zf.open(lo_name) as lo_raw:
            hd = io.TextIOWrapper(hd_raw, encoding="latin-1", newline="")
            lo = io.TextIOWrapper(lo_raw, encoding="latin-1", newline="")
            return build_locations(hd, lo)


def row_tuple(loc: dict) -> tuple:
    values = []
    for col in INSERT_COLUMNS:
        value = loc.get(col)
        if col == "call_sign":
            value = (value or "")[:10]
        values.append(value)
    return tuple(values)


def ddl_batches(text: str) -> list[str]:
    """Split a T-SQL script on GO lines (comments-only batches dropped)."""
    batches = []
    for chunk in re.split(r"(?im)^\s*GO\s*$", text):
        body = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body:
            batches.append(body)
    return batches


def load(rows: list[tuple], *, batch_size: int) -> None:
    from enrichment.mssql import connect_mssql

    conn = connect_mssql()
    try:
        cursor = conn.cursor()
        for batch in ddl_batches(DDL_PATH.read_text(encoding="utf-8")):
            cursor.execute(batch)
        conn.commit()
        try:
            cursor.fast_executemany = True
        except AttributeError:
            pass
        cols = ", ".join(INSERT_COLUMNS)
        marks = ", ".join("?" for _ in INSERT_COLUMNS)
        insert = f"INSERT INTO {STAGING_TABLE} ({cols}) VALUES ({marks})"
        started = time.time()
        for start in range(0, len(rows), batch_size):
            cursor.executemany(insert, rows[start : start + batch_size])
            conn.commit()
            done = min(len(rows), start + batch_size)
            print(f"staged {done}/{len(rows)} ({time.time() - started:.0f}s)")
        cursor.execute(f"SELECT COUNT(*) FROM {STAGING_TABLE}")
        staged = cursor.fetchone()[0]
        if staged != len(rows):
            raise RuntimeError(f"staging count {staged} != parsed {len(rows)}; target untouched")
        cursor.execute(f"DELETE FROM {ULS_TABLE}")
        cursor.execute(
            f"INSERT INTO {ULS_TABLE} ({cols}, loaded_at) "
            f"SELECT {cols}, SYSUTCDATETIME() FROM {STAGING_TABLE}"
        )
        conn.commit()
        cursor.execute(f"DROP TABLE {STAGING_TABLE}")
        conn.commit()
        cursor.execute(f"SELECT COUNT(*) FROM {ULS_TABLE}")
        print(f"done — {ULS_TABLE} rows={cursor.fetchone()[0]}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", type=Path, help="Local l_micro.zip (skip download)")
    parser.add_argument("--url", default=FCC_ULS_MICRO_URL, help="Download URL when --zip is not given")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print counts; no SQL")
    parser.add_argument("--batch-size", type=int, default=5000, help="executemany batch size")
    args = parser.parse_args(argv)

    path = args.zip
    if path is None:
        path = default_zip_path()
        download(args.url, path)
    if not path.is_file():
        print(f"zip not found: {path}", file=sys.stderr)
        return 2
    started = time.time()
    locations, stats = parse_zip(path)
    print(f"parsed in {time.time() - started:.0f}s: " + " ".join(f"{k}={v}" for k, v in stats.items()))
    services: dict[str, int] = {}
    for loc in locations:
        code = loc.get("radio_service_code") or "?"
        services[code] = services.get(code, 0) + 1
    top = sorted(services.items(), key=lambda kv: -kv[1])[:10]
    print("radio services: " + ", ".join(f"{k}={v}" for k, v in top))
    rows = [row_tuple(loc) for loc in locations]
    if args.dry_run:
        print(f"dry-run — {len(rows)} row(s) would load into {ULS_TABLE}; no SQL")
        return 0
    load(rows, batch_size=max(1, args.batch_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
