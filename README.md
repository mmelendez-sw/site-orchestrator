# Site Orchestrator

Salesforce enrichment: pull blank `Site_Type__c` sites, snap to FCC/TowerSource, classify from NAIP + optional Nearmap with Gemini/Claude (OSM prefilter before paid imagery), then write each qualifying site back to Salesforce before the next classify. An end-of-run sweep applies anything still pending.

There is no upload-template or CSV-import step. Run CSVs under `../site-orchestrator-data/runs/` are an audit log.

```
python -m enrichment
```

Set `APPLY=0` to classify and write CSVs without Salesforce updates. Optional env: `STATES`, `STAGES`, `LIMIT`, `OFFSET`, `SKIP_FROM`, `IDS`, `CARRIER_LIKE`, `METRO_CLASSIFICATION`, `LLM_CLASSIFIED`, `RUN_DIR`, `VERBOSE`, `METRICS_SQL`, `DEQUEUE_HOLDOUTS`, `DB_ONLY`, `CONFIRM_ROOFTOP`. The queue defaults to `Stage__c = 'Outreach - Verified'` with no `LIMIT`. Set `STAGES` to a comma-separated picklist list to widen it.

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

Leadership KPIs land in Azure SQL (`dbo.EnrichmentRun` / `dbo.EnrichmentSiteOutcome`) at the end of `APPLY=1` runs. UniqueSites is distinct Salesforce Ids with `sf_update_status=updated` (a real site-type/coords write), including rooftop-confirm applies. `APPLY=0` dry-runs do not increment UniqueSites or rewrite `kpis.json`. DB-only unique hits count only when that write succeeds; misses, holdouts, and retries of an Id already counted stay off UniqueSites. Details: [docs/enrichment-metrics.md](docs/enrichment-metrics.md).

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
├── enrichment/     # pull → proximity → classify → apply → metrics upsert
├── classifier/     # NAIP + Nearmap imagery, Gemini + Claude, OSM prefilter
├── salesforce/     # auth + Site_Type picklist mapping
├── sql/            # enrichment metrics DDL (Symphony_dev)
├── docs/           # metrics schema and queries
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

Also install the ODBC Driver 18 for SQL Server. Azure SQL uses Entra token auth (`az login`); `pyodbc` and `azure-identity` are in `requirements.txt`.

## Classify path

1. **NAIP + Gemini** — always, unless a unique FCC/TowerSource hit ≤ 25 m skips imagery.
2. **OSM Overpass** — cheap prefilter before Nearmap when the NAIP pass is inconclusive.
3. **Nearmap** vert + obliques — rooftops and towers that still need high-res sides.
4. **Claude** — dual-model cell confirm (skipped for high-conf Gemini towers ≥ 0.85, and below 0.7 site confidence).

After each Gemini classify the process sleeps `GEMINI_DELAY_S` (default 8s; override in `.env`). Run **at most two** `python -m enrichment` classify jobs at once. A third job shares the same Gemini quota (more 429 retries) and Azure SQL pool. Do not run `load_enrichment_metrics.py` during those jobs. Set `METRICS_SQL=0` on the classify terminals if Azure SQL is dropping; reload KPIs later. Live processes keep the delay they started with — Ctrl+C and restart to pick up a new default. Per-site apply already wrote finished sites.

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
