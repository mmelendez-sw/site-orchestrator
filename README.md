# Site Orchestrator

Salesforce enrichment: pull blank `Site_Type__c` sites, snap to FCC/TowerSource, classify from NAIP + optional Nearmap with Gemini/Claude (OSM prefilter before paid imagery), then write qualifying results back to Salesforce in the same run.

There is no upload-template or CSV-import step. Run CSVs under `../site-orchestrator-data/runs/` are an audit log.

```
python -m enrichment
```

Set `APPLY=0` to classify and write CSVs without Salesforce updates. Optional env: `STATES`, `STAGES`, `LIMIT`, `OFFSET`, `SKIP_FROM`, `IDS`, `CARRIER_LIKE`, `METRO_CLASSIFICATION`, `LLM_CLASSIFIED`, `RUN_DIR`, `VERBOSE`, `METRICS_SQL`, `DEQUEUE_HOLDOUTS`, `DB_ONLY`. The queue defaults to `Stage__c = 'Outreach - Verified'` with no `LIMIT`. Set `STAGES` to a comma-separated picklist list to widen it.

`CARRIER_LIKE` is the `Carrier_Leasing_Source__c` LIKE needle (unset = no carrier filter). Set `CARRIER_LIKE=NFL` to restrict to NFL sources. `METRO_CLASSIFICATION` is an exact `Metro_Classification__c` match (default `Major NFL Metro`). Set `METRO_CLASSIFICATION=none` to omit it. Enrichment does not write those fields. The queue defaults to `LLM_Classified__c = false`; set `LLM_CLASSIFIED=1` only to re-pull already-flagged rows.

**DB-only (no Nearmap):** `DB_ONLY=1` walks blank-`Site_Type` sites in **New/Unreviewed, Enhanced/Unreviewed, Outreach, Outreach - Verified, Marketing**, any owner. Every processed site is marked `LLM_Classified=true` (no `LLM_Holdout`). Unique FCC/TowerSource hits also write site type and coords. Misses stay blank type and classified true until you flip them (`LLM_CLASSIFIED=1`, then set classified false). `SKIP_FROM` still skips Ids already in prior run CSVs. Change stages with `STAGES` or `LEAD_STAGES`.

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

Leadership KPIs land in Azure SQL (`dbo.EnrichmentRun` / `dbo.EnrichmentSiteOutcome`) at the end of `APPLY=1` runs. DB-only unique hits roll into cumulative KPIs; DB-only misses stay on the this-run header only. Details: [docs/enrichment-metrics.md](docs/enrichment-metrics.md).

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
4. **Claude** — dual-model cell confirm (skipped for high-conf Gemini towers ≥ 0.9, and below 0.7 site confidence).

## What it writes

- **Towers:** Gemini tower + cell at site confidence ≥ 0.9 auto-apply (imagery-only allowed). Claude can confirm weaker towers.
- **Rooftops:** Nearmap obliques + dual-model agreement, or NAIP-only when site and cell confidence ≥ 0.95 with named gear, unhedged evidence, and an asset box. Otherwise holdout and dequeue (`LLM_Classified=false`, `LLM_Holdout=true`).
- Unique FCC/TowerSource hit ≤ 25 m skips imagery and still updates coords + verified source.
