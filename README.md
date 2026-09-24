# Site Orchestrator

Salesforce enrichment: pull blank `Site_Type__c` sites, snap to FCC/TowerSource, classify from NAIP + optional Nearmap with Gemini/Claude (OSM prefilter before paid imagery), then write each qualifying site back to Salesforce as soon as it finishes. An end-of-run sweep applies anything still pending.

There is no upload-template or CSV-import step. Run CSVs under `../site-orchestrator-data/runs/` are an audit log.

```
python -m enrichment
```

Set `APPLY=0` to classify and write CSVs without Salesforce updates. Optional env: `STATES`, `STAGES`, `LIMIT`, `OFFSET`, `SKIP_FROM`, `IDS`, `CARRIER_LIKE`, `METRO_CLASSIFICATION`, `LLM_CLASSIFIED`, `RUN_DIR`, `VERBOSE`, `METRICS_SQL`, `DEQUEUE_HOLDOUTS`, `DB_ONLY`, `CONFIRM_ROOFTOP`, `CLASSIFY_WORKERS`, `APPLY_BATCH_SIZE`. `SKIP_FROM=sql` skips every Id a live run already recorded in Azure SQL (combine with run folders / dates by comma). The queue defaults to `Stage__c = 'Outreach - Verified'` with no `LIMIT`. Set `STAGES` to a comma-separated picklist list to widen it.

`CARRIER_LIKE` is the `Carrier_Leasing_Source__c` LIKE needle (unset = no carrier filter). Set `CARRIER_LIKE=NFL` to restrict to NFL sources. `METRO_CLASSIFICATION` is an exact `Metro_Classification__c` match (default `Major NFL Metro`). Set `METRO_CLASSIFICATION=none` to omit it. Enrichment does not write those fields. The queue defaults to `LLM_Classified__c = false`; set `LLM_CLASSIFIED=1` only to re-pull already-flagged rows.

**DB-only (no Nearmap):** `DB_ONLY=1` walks blank-`Site_Type` sites in **New/Unreviewed, Enhanced/Unreviewed, Outreach, Outreach - Verified, Marketing**, any owner. Successful writes are `LLM_Classified=true` (no `LLM_Holdout`). Unique FCC/TowerSource hits also write site type and coords. Misses stay blank type and classified true until you flip them (`LLM_CLASSIFIED=1`, then set classified false). If a Salesforce update fails (duplicates, API errors), the apply retries once with `LLM_Classified=false` and `LLM_Holdout=true` so the site leaves the queue. `SKIP_FROM` still skips Ids already in prior run CSVs. Change stages with `STAGES` or `LEAD_STAGES`.

```powershell
$env:DB_ONLY="1"
$env:METRO_CLASSIFICATION="none"
$env:STAGES="New/Unreviewed,Enhanced/Unreviewed,Outreach,Outreach - Verified,Marketing"
$env:LIMIT="200"
$env:SKIP_FROM="2026-09-09"
$env:APPLY="0"
$env:VERBOSE="1"
python -m enrichment
```

Then set `APPLY=1` for live Salesforce writes. `Working-Connected` / `Qualified (Converted)` stay excluded unless listed in `STAGES`.

Leadership KPIs land in Azure SQL (`dbo.EnrichmentRun` / `dbo.EnrichmentSiteOutcome`) during `APPLY=1` runs: the run header at start, each site row as that site finishes, and a full reconcile at the end. **UniqueSites** is every distinct Salesforce Id a live run processed — applied, held out, DB-only miss, failed apply, or error. **WrittenSites** is the subset Salesforce accepted a site-type/coords write for (`sf_update_status=updated`, including rooftop-confirm applies). Retries of the same Id count once (latest outcome wins). `APPLY=0` dry-runs never touch the cumulative KPIs or `kpis.json`. Details: [docs/enrichment-metrics.md](docs/enrichment-metrics.md).

**NAIP rooftop confirm:** `CONFIRM_ROOFTOP=1` pulls existing `Site_Type=Rooftop` (not blank type), classifies NAIP + Gemini for **building-roof presence** (not cellular gear), and writes `Site_Type` + `LLM_Classified=true` when NAIP labels rooftop. HVAC-only or empty roofs still confirm. Inconclusive rows are left unchanged. No Nearmap, no Claude, no cell-gear bar. Successful applies count as rooftop SF writes / UniqueSites. Default stages include Working-Connected; set `CARRIER_LIKE` as needed. Uncomment `LIMIT` in `.env` only to cap a slice. Dry-run with `APPLY=0` first.

```powershell
$env:CONFIRM_ROOFTOP="1"
$env:CARRIER_LIKE="ConnectX"
$env:METRO_CLASSIFICATION="none"
$env:OWNERS="none"
$env:APPLY="0"
$env:VERBOSE="1"
python -m enrichment
```

Salesforce apply is stage 4/4. If you Ctrl+C during classify, push the paused CSV (no Gemini rerun):

```powershell
$env:CONFIRM_ROOFTOP="1"
$env:APPLY="1"
$env:APPLY_EXISTING="1"
$env:RUN_DIR="C:\Users\Mmelendez\Codebases\site-orchestrator-data\runs\2026-09-17_130656_sf_enrichment"
$env:VERBOSE="1"
python -m enrichment
```

## Layout

```
site-orchestrator/
├── enrichment/     # queue → prepare (geocode + bulk proximity) → classify → apply → metrics
├── classifier/     # decision gates (asset_classifier), prompts, imagery, llm, views, evidence
├── salesforce/     # auth + Site_Type picklist mapping
├── sql/            # enrichment metrics DDL (Symphony_dev)
├── scripts/        # metrics loader/backfill, decision replay
├── docs/           # metrics schema and queries
├── envutil.py      # typed env readers
├── paths.py        # sibling data folder
└── requirements.txt
```

CSVs, `runs/`, and `chips/` live in **`../site-orchestrator-data`** (not in git). Override with `SITE_ORCHESTRATOR_DATA`.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # fill in credentials
```

Also install the ODBC Driver 18 for SQL Server. For tests and lint: `pip install -r requirements-dev.txt`, then `python -m pytest -q` (offline — no paid APIs, Salesforce, or SQL). CI runs the same on every push (`.github/workflows/tests.yml`). Azure SQL uses Entra token auth (`az login`); `pyodbc` and `azure-identity` are in `requirements.txt`.

## Classify path

1. **NAIP + Gemini** — always, unless a unique FCC/TowerSource hit ≤ 25 m skips imagery.
2. **OSM Overpass** — cheap prefilter before Nearmap when the NAIP pass is inconclusive.
3. **Nearmap** vert + obliques — rooftops and towers that still need high-res sides.
4. **Claude** — dual-model cell confirm (skipped for high-conf Gemini towers ≥ 0.85, and below 0.7 site confidence).

### Throughput and pacing

One process classifies `CLASSIFY_WORKERS` sites at once (default 3). Pins within `PIN_CLUSTER_M` of each other stay in one worker so nearby-pin reuse still saves imagery. Every Gemini / Claude call waits on a shared limiter — `GEMINI_RPM` (default 30) and `CLAUDE_RPM` (default 50) — and a 429/503 pauses all workers together. This replaces the old fixed `GEMINI_DELAY_S` sleep after each site (`GEMINI_DELAY_S` / `CLAUDE_DELAY_S` are ignored now). Run **one** terminal and raise `CLASSIFY_WORKERS` instead of opening more; separate processes do not share the limiter. Lower `GEMINI_RPM` if 429 retries show up.

Before classifying, the run geocodes the whole queue in Census batch requests (cached in `../site-orchestrator-data/cache/census_geocode.jsonl`) and looks up FCC/TowerSource for all pins in a few temp-table joins instead of two to four queries per site. If the bulk lookup fails, sites fall back to per-site queries automatically.

Salesforce writes, Azure SQL, and the detail CSV all stay on the main thread. The first Ctrl+C finishes in-flight sites and flushes pending writes; a second aborts. Salesforce writes go out in sObject Collections batches of `APPLY_BATCH_SIZE` (default 25, max 200) or after `APPLY_FLUSH_S` (default 60 s), whichever comes first; `APPLY_BATCH_SIZE=1` writes each site as it finishes. Every finished site is in `enrichment_detail.csv` (status `pending`) before its batch is sent, so after a hard crash `APPLY_EXISTING=1` with that `RUN_DIR` pushes whatever had not gone out.

Imagery is cached under `../site-orchestrator-data/cache/`: NAIP chips per STAC item + point, Nearmap tiles per survey capture date (a new survey refetches). Reruns, wide AOIs, re-centers, and neighbors reuse pixels instead of re-downloading. Set `IMAGERY_CACHE=0` to disable; check your Nearmap agreement on how long tiles may be retained. Do not run `load_enrichment_metrics.py` during a classify run. Set `METRICS_SQL=0` if Azure SQL is dropping; reload KPIs later.

### Checking a rule change

Before shipping a threshold or gate change, replay recent classifications through the new rules (no imagery, models, or Salesforce):

```powershell
python scripts/replay_decisions.py 2026-09
```

It prints every site whose bucket / holdout reason / Site_Type would change and exits 1 if any did.

Re-score holdouts on already-purchased Nearmap JPEGs (no new Nearmap fetch; Gemini and Claude still run). Dry-run a slice first:

```powershell
$env:RERUN_HOLDOUTS_FROM="2026-09-03"
$env:REUSE_CHIPS_FROM="2026-09-03"
$env:LIMIT="50"
$env:APPLY="0"
$env:VERBOSE="1"
python -m enrichment
```

Sites with no saved chips return `error=no_saved_chips`. Then `APPLY=1` if the holdout mix looks right.

## What it writes

- **Towers:** Gemini tower + cell at site confidence ≥ 0.85 auto-apply (imagery-only allowed). Claude can confirm weaker towers.
- **Rooftops:** Nearmap obliques + dual-model agreement, or NAIP-only when site and cell confidence ≥ 0.95 with named gear, unhedged evidence, and an asset box. Otherwise holdout and dequeue (`LLM_Classified=false`, `LLM_Holdout=true`).
- Unique FCC/TowerSource hit ≤ 25 m skips imagery and still updates coords + verified source.
