"""Load Overture Maps building footprints near Salesforce sites into dbo.OvertureBuilding.

Only buildings within RADIUS_M (default 100 m) of a Site__c pin are kept, so a
160k-site org needs a few GB of the ~277 GB global buildings release. Overture
publishes each release for ~60 days; re-run all steps after a new release.

  python scripts/load_overture_buildings.py sites      # Site__c pins -> overture/sites.csv (read-only SOQL)
  python scripts/load_overture_buildings.py plan       # pick row groups near sites (metadata only)
  python scripts/load_overture_buildings.py extract    # download those row groups, keep buildings <= 100 m (resumable)
  python scripts/load_overture_buildings.py load       # sql/overture_buildings.sql, stage, swap into the target

Files live under SITE_ORCHESTRATOR_DATA/overture/<release>/. Uses the same
Entra token SQL connection as the other loaders (az login). Data: Overture
Maps Foundation, ODbL; derived records need attribution.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from paths import data_root  # noqa: E402

STAC_ROOT = "https://stac.overturemaps.org"
DDL_PATH = ROOT / "sql" / "overture_buildings.sql"
TABLE = "dbo.OvertureBuilding"
STAGING_TABLE = "dbo.OvertureBuilding_Staging"
RADIUS_M = 100.0
M_PER_DEG = 111_320.0
PLAN_CELL_DEG = 0.01   # site grid for row-group selection (~1 km)
JOIN_CELL_DEG = 0.005  # site grid for the per-building distance join (~550 m)
READ_COLUMNS = ("id", "bbox", "geometry", "height", "num_floors", "class", "subtype")
INSERT_COLUMNS = (
    "building_id", "centroid_lat", "centroid_lon", "xmin", "ymin", "xmax", "ymax",
    "area_m2", "height_m", "num_floors", "building_class", "subtype", "shape_wkb",
    "overture_release",
)


# ------------------------------------------------------------------ helpers


def overture_dir() -> Path:
    return data_root() / "overture"


def latest_release() -> str:
    with urllib.request.urlopen(f"{STAC_ROOT}/catalog.json", timeout=60) as resp:
        return json.load(resp)["latest"]


def release_dir(release: str) -> Path:
    path = overture_dir() / release
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_sites(path: Path) -> list[tuple[float, float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [(float(r["lat"]), float(r["lon"])) for r in csv.DictReader(handle)]


def site_grid(sites: list[tuple[float, float]], cell: float) -> dict[tuple[int, int], list[tuple[float, float]]]:
    grid: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for lat, lon in sites:
        grid[(math.floor(lat / cell), math.floor(lon / cell))].append((lat, lon))
    return grid


def box_near_site(
    grid: dict[tuple[int, int], list[tuple[float, float]]],
    cell: float,
    xmin: float, ymin: float, xmax: float, ymax: float,
    radius_m: float = RADIUS_M,
) -> bool:
    """True when any site lies within ``radius_m`` of the lon/lat box (padded box test)."""
    pad_lat = radius_m / M_PER_DEG
    pad_lon = pad_lat / max(0.2, math.cos(math.radians((ymin + ymax) / 2)))
    for i in range(math.floor((ymin - pad_lat) / cell), math.floor((ymax + pad_lat) / cell) + 1):
        for j in range(math.floor((xmin - pad_lon) / cell), math.floor((xmax + pad_lon) / cell) + 1):
            for lat, lon in grid.get((i, j), ()):
                if ymin - pad_lat <= lat <= ymax + pad_lat and xmin - pad_lon <= lon <= xmax + pad_lon:
                    return True
    return False


def ddl_batches(text: str) -> list[str]:
    import re

    batches = []
    for chunk in re.split(r"(?im)^\s*GO\s*$", text):
        body = "\n".join(line for line in chunk.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            batches.append(body)
    return batches


# ------------------------------------------------------------------- steps


def cmd_sites(_args) -> int:
    from enrichment.sf_ops import query_all
    from salesforce.sf_client import SalesforceClient

    rows = query_all(
        SalesforceClient(),
        "SELECT Id, Site_Latitude__c, Site_Longitude__c FROM Site__c "
        "WHERE Site_Latitude__c != null AND Site_Longitude__c != null",
    )
    out = overture_dir() / "sites.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    kept = bad = 0
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Id", "lat", "lon"])
        for row in rows:
            try:
                lat, lon = float(row["Site_Latitude__c"]), float(row["Site_Longitude__c"])
            except (TypeError, ValueError):
                bad += 1
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                bad += 1
                continue
            writer.writerow([row["Id"], lat, lon])
            kept += 1
    print(f"{kept} site pins -> {out} ({bad} bad coordinates skipped)")
    return 0


def _items(release: str) -> list[dict]:
    url = f"{STAC_ROOT}/{release}/buildings/building/collection.json"
    with urllib.request.urlopen(url, timeout=60) as resp:
        col = json.load(resp)
    base = url.rsplit("/", 1)[0] + "/"
    hrefs = [link["href"] for link in col["links"] if link.get("rel") == "item"]
    hrefs = [h if h.startswith("http") else base + h.lstrip("./") for h in hrefs]

    def get(href: str) -> dict:
        with urllib.request.urlopen(href, timeout=60) as resp:
            item = json.load(resp)
        asset = item["assets"].get("aws") or next(iter(item["assets"].values()))
        return {"id": item["id"], "bbox": item["bbox"], "href": asset["href"]}

    with ThreadPoolExecutor(16) as pool:
        return list(pool.map(get, hrefs))


def cmd_plan(args) -> int:
    import duckdb

    release = args.release or latest_release()
    sites = read_sites(overture_dir() / "sites.csv")
    grid = site_grid(sites, PLAN_CELL_DEG)
    items = [
        it for it in _items(release)
        if box_near_site(grid, PLAN_CELL_DEG, *it["bbox"], radius_m=1000.0)
    ]
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    plan = []
    started = time.time()
    for k, item in enumerate(items, 1):
        rows = con.execute(f"""
          SELECT row_group_id, ANY_VALUE(row_group_num_rows), SUM(total_compressed_size),
            MIN(CASE WHEN path_in_schema = 'bbox, xmin' THEN TRY_CAST(stats_min AS DOUBLE) END),
            MIN(CASE WHEN path_in_schema = 'bbox, ymin' THEN TRY_CAST(stats_min AS DOUBLE) END),
            MAX(CASE WHEN path_in_schema = 'bbox, xmax' THEN TRY_CAST(stats_max AS DOUBLE) END),
            MAX(CASE WHEN path_in_schema = 'bbox, ymax' THEN TRY_CAST(stats_max AS DOUBLE) END)
          FROM parquet_metadata('{item['href']}') GROUP BY 1 ORDER BY 1""").fetchall()
        for rg, n, size, x0, y0, x1, y1 in rows:
            if None in (x0, y0, x1, y1):
                continue
            if box_near_site(grid, PLAN_CELL_DEG, x0, y0, x1, y1):
                plan.append({"file": item["id"], "href": item["href"], "rg": rg, "rows": n, "bytes": size})
        if k % 10 == 0:
            print(f"  {k}/{len(items)} files ({time.time() - started:.0f}s)", flush=True)
    out = release_dir(release) / "rowgroup_plan.json"
    out.write_text(json.dumps(plan), encoding="utf-8")
    print(f"release {release}: {len(items)} files, {len(plan)} row groups, "
          f"{sum(p['rows'] for p in plan):,} buildings, {sum(p['bytes'] for p in plan) / 1e9:.1f} GB compressed -> {out}")
    return 0


_local = threading.local()


def _duck(sites_path: Path):
    """Per-thread DuckDB with spatial + the site cell table (3x3 neighbours)."""
    con = getattr(_local, "con", None)
    if con is None:
        import duckdb

        con = duckdb.connect()
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute(f"""
          CREATE TABLE site_cells AS
          WITH s AS (SELECT CAST(lat AS DOUBLE) lat, CAST(lon AS DOUBLE) lon
                     FROM read_csv_auto('{sites_path.as_posix()}'))
          SELECT CAST(floor(lat / {JOIN_CELL_DEG}) AS BIGINT) + di AS ci,
                 CAST(floor(lon / {JOIN_CELL_DEG}) AS BIGINT) + dj AS cj, lat, lon
          FROM s, (SELECT unnest([-1, 0, 1]) di), (SELECT unnest([-1, 0, 1]) dj)""")
        _local.con = con
    return con


NEAR_SQL = f"""
WITH bb AS (
  SELECT id, bbox.xmin x0, bbox.ymin y0, bbox.xmax x1, bbox.ymax y1, geometry, height, num_floors,
         class, subtype,
         CAST(floor(((bbox.ymin + bbox.ymax) / 2) / {JOIN_CELL_DEG}) AS BIGINT) ci,
         CAST(floor(((bbox.xmin + bbox.xmax) / 2) / {JOIN_CELL_DEG}) AS BIGINT) cj
  FROM rg),
near AS (
  SELECT DISTINCT bb.id FROM bb JOIN site_cells s ON s.ci = bb.ci AND s.cj = bb.cj
  WHERE sqrt(power(greatest(bb.x0 - s.lon, 0, s.lon - bb.x1) * {M_PER_DEG} * cos(radians(s.lat)), 2)
           + power(greatest(bb.y0 - s.lat, 0, s.lat - bb.y1) * {M_PER_DEG}, 2)) <= {RADIUS_M})
SELECT bb.id AS building_id,
       ST_Y(ST_Centroid(g)) AS centroid_lat, ST_X(ST_Centroid(g)) AS centroid_lon,
       bb.x0 AS xmin, bb.y0 AS ymin, bb.x1 AS xmax, bb.y1 AS ymax,
       ST_Area(g) * {M_PER_DEG} * {M_PER_DEG} * cos(radians((bb.y0 + bb.y1) / 2)) AS area_m2,
       bb.height AS height_m, CAST(bb.num_floors AS SMALLINT) AS num_floors,
       bb.class AS building_class, bb.subtype, bb.geometry AS shape_wkb
FROM bb JOIN near USING (id), LATERAL (SELECT ST_GeomFromWKB(bb.geometry) AS g)
"""


def _extract_one(job: dict, sites_path: Path, parts: Path, s3) -> int:
    import pyarrow.parquet as pq

    out = parts / f"{job['file']}_{job['rg']:04d}.parquet"
    if out.exists():
        return -1
    key = job["href"].split(".amazonaws.com/", 1)[1]
    path = "overturemaps-us-west-2/" + key
    for attempt in range(4):
        try:
            pf = pq.ParquetFile(path, filesystem=s3)
            cols = [c for c in READ_COLUMNS if c in pf.schema_arrow.names]
            rg = pf.read_row_group(job["rg"], columns=cols)  # noqa: F841 (DuckDB scans it)
            break
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    con = _duck(sites_path)
    con.register("rg", rg)
    table = con.execute(NEAR_SQL).to_arrow_table()
    con.unregister("rg")
    tmp = out.with_suffix(".tmp")
    pq.write_table(table, tmp)
    tmp.replace(out)
    return table.num_rows


def cmd_extract(args) -> int:
    from pyarrow import fs

    release = args.release or latest_release()
    rdir = release_dir(release)
    plan = json.loads((rdir / "rowgroup_plan.json").read_text(encoding="utf-8"))
    parts = rdir / "parts"
    parts.mkdir(exist_ok=True)
    sites_path = overture_dir() / "sites.csv"
    s3 = fs.S3FileSystem(anonymous=True, region="us-west-2")
    started = time.time()
    kept = skipped = done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_extract_one, job, sites_path, parts, s3) for job in plan]
        for fut in as_completed(futures):
            n = fut.result()
            done += 1
            if n < 0:
                skipped += 1
            else:
                kept += n
            if done % 200 == 0 or done == len(plan):
                print(f"  {done}/{len(plan)} row groups, {kept:,} buildings kept, "
                      f"{skipped} already done ({time.time() - started:.0f}s)", flush=True)
    print(f"extract done: {kept:,} new buildings near sites -> {parts}")
    return 0


def cmd_load(args) -> int:
    import duckdb

    from enrichment.mssql import connect_mssql

    release = args.release or latest_release()
    parts = release_dir(release) / "parts"
    con = duckdb.connect()
    con.execute(f"""
      CREATE TABLE b AS
      SELECT building_id, centroid_lat, centroid_lon, xmin, ymin, xmax, ymax, area_m2, height_m,
             num_floors, building_class, subtype, shape_wkb
      FROM read_parquet('{parts.as_posix()}/*.parquet')
      QUALIFY row_number() OVER (PARTITION BY building_id) = 1""")
    total = con.execute("SELECT COUNT(*) FROM b").fetchone()[0]
    print(f"{total:,} unique buildings from {parts}")
    if args.dry_run:
        return 0
    reader = con.execute("SELECT * FROM b")
    conn = connect_mssql()
    try:
        cursor = conn.cursor()
        for batch in ddl_batches(DDL_PATH.read_text(encoding="utf-8")):
            cursor.execute(batch)
        conn.commit()
        cursor.fast_executemany = True
        cols = ", ".join(INSERT_COLUMNS)
        marks = ", ".join("?" for _ in INSERT_COLUMNS)
        insert = f"INSERT INTO {STAGING_TABLE} ({cols}) VALUES ({marks})"
        started = time.time()
        done = 0
        while True:
            batch = reader.fetchmany(args.batch_size)
            if not batch:
                break
            cursor.executemany(insert, [(*r, release) for r in batch])
            conn.commit()
            done += len(batch)
            if done % (args.batch_size * 20) < args.batch_size or done == total:
                print(f"  staged {done:,}/{total:,} ({time.time() - started:.0f}s)", flush=True)
        cursor.execute(f"SELECT COUNT(*) FROM {STAGING_TABLE}")
        staged = cursor.fetchone()[0]
        if staged != total:
            raise RuntimeError(f"staging count {staged} != {total}; target untouched")
        target_cols = ", ".join(c for c in INSERT_COLUMNS if c != "shape_wkb").replace(
            "overture_release", "shape, overture_release")
        select_cols = ", ".join(c for c in INSERT_COLUMNS if c != "shape_wkb").replace(
            "overture_release", "geometry::STGeomFromWKB(shape_wkb, 4326).MakeValid(), overture_release")
        cursor.execute(f"TRUNCATE TABLE {TABLE}")
        cursor.execute(f"INSERT INTO {TABLE} ({target_cols}) SELECT {select_cols} FROM {STAGING_TABLE}")
        conn.commit()
        cursor.execute(f"DROP TABLE {STAGING_TABLE}")
        conn.commit()
        cursor.execute(f"SELECT COUNT(*) FROM {TABLE}")
        print(f"done: {TABLE} rows={cursor.fetchone()[0]:,} ({time.time() - started:.0f}s)")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["sites", "plan", "extract", "load"])
    parser.add_argument("--release", help="Overture release (default: latest from the STAC catalog)")
    parser.add_argument("--workers", type=int, default=12, help="extract: parallel row-group downloads")
    parser.add_argument("--batch-size", type=int, default=5000, help="load: executemany batch size")
    parser.add_argument("--dry-run", action="store_true", help="load: count rows only, no SQL")
    args = parser.parse_args(argv)
    return {"sites": cmd_sites, "plan": cmd_plan, "extract": cmd_extract, "load": cmd_load}[args.step](args)


if __name__ == "__main__":
    raise SystemExit(main())
