# Site Orchestrator

End-to-end pipeline for wireless site sales intelligence: discover candidate sites from open permit data, normalize and geocode them, dedupe against Salesforce, classify from aerial imagery, and load net-new records.

```
source → ingest → dedupe → classifier → salesforce
```

## Repository layout

```
site-orchestrator/
├── orchestrator.py          # wires the full pipeline
├── requirements.txt
├── .env.example
├── source/                  # permit discovery (gov data, CSV/JSON, scripts)
├── ingest/                  # geocode + normalize to canonical records
├── dedupe/                  # Salesforce spatial + fuzzy dedupe
├── classifier/              # NAIP/Nearmap imagery + Claude classification
├── salesforce/              # create sites + duplicate audit logging
├── data/                    # input CSVs (gitignored)
├── runs/                    # runtime outputs (gitignored)
└── chips/                   # saved imagery chips (gitignored)
```

Classifier-specific docs live in [`classifier/docs/`](classifier/docs/) (configuration, workflow guides, diagrams).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # fill in credentials
```

Key environment variables:

| Variable | Used by | Purpose |
|----------|---------|---------|
| `SF_USERNAME`, `SF_PASSWORD`, `SF_SECURITY_TOKEN` | dedupe, salesforce | Salesforce API auth |
| `GEOCODE_API_KEY` | ingest | Google Maps geocoding |
| `ANTHROPIC_API_KEY` | classifier | Claude vision classification |
| `NEARMAP_API_KEY` | classifier | Optional high-res oblique imagery |

See [`.env.example`](.env.example) for the full list.

## Pipeline stages

### 1. Source — discover candidate sites

Generate site lists from open permit portals, Python scripts, or Claude-generated CSV/JSON. Geographic scope accepts **country, state, county, city, and zip codes**.

```powershell
# List adapters compatible with a scope
python -m source.runner --list-sources --state WI --city Milwaukee

# Milwaukee open-data permits → assets CSV
python -m source.runner milwaukee_permits --state WI --city Milwaukee --output-csv data/WI_assets.csv

# Any hand-built or AI-generated file
python -m source.runner file --input data/candidates.csv --state WI --zip 53202,53203 --output-csv data/WI_assets.csv

# Export JSON for Claude review
python -m source.runner file --input data/candidates.json --output-json data/candidates.json
```

**Registered sources:** `milwaukee_permits`, `file` (CSV/JSON). Add new jurisdictions by implementing `BaseSourceAdapter` in `source/adapters/`.

### 2. Ingest — normalize records

Each raw record is geocoded (if needed), reverse-geocoded (if needed), or validated for address/coordinate alignment. Output is a canonical dict: `lat`, `lng`, `address`, `zip_code`, `permit_metadata`.

### 3. Dedupe — match against Salesforce

Before matching, the orchestrator **prefetches** Salesforce candidates for the entire batch:

1. All **zip codes** found in the dataset
2. One bounding box from the dataset **min/max lat/lng**, expanded by **±250m** (dataset-wide, not per site)

SOQL uses `Site_Latitude__c`, `Site_Longitude__c`, and `Zip_Code__c`. Fuzzy address matching (`rapidfuzz`) assigns each record a status:

| Status | Meaning |
|--------|---------|
| `duplicate` | High-confidence match — logged, skipped |
| `review` | Medium-confidence match — written to `runs/review_log.csv` |
| `net_new` | No match — proceeds to classification |

### 4. Classifier — aerial imagery classification

Net-new records are classified using NAIP (+ optional Nearmap) imagery and Claude vision. Runs standalone or via the orchestrator.

```powershell
python classifier/asset_classifier.py -i data/WI_assets.csv
```

### 5. Salesforce — load net-new sites

Classified net-new records are created in Salesforce via `Site__c` field mappings in `salesforce/field_map.py`.

## Run the full orchestrator

```powershell
# Source → dedupe only
python orchestrator.py --source milwaukee_permits --state WI --city Milwaukee

# Source → dedupe → classify → Salesforce load
python orchestrator.py --source file --input data/WI_assets.csv --state WI --classify

# Dedupe an in-memory list from Python
python -c "from orchestrator import run_from_source; run_from_source('milwaukee_permits', classify=False)"
```

## Data files

Input CSVs belong in `data/` (gitignored). Classifier expects:

```csv
id,address,label,input_confidence
wi_001,"100 E PLEASANT ST, MILWAUKEE, WI 53212",WI,high
```

Runtime outputs (`runs/`, `chips/`, `results.csv`) are gitignored and stay local.

## Branch workflow

- **`main`** — stable
- **`dev`** — active development

## Further reading

- [`classifier/docs/README.md`](classifier/docs/README.md) — classifier setup and run details
- [`classifier/docs/CONFIGURATION.md`](classifier/docs/CONFIGURATION.md) — pipeline flags and model settings
- [`classifier/docs/WORKFLOW_GUIDE.md`](classifier/docs/WORKFLOW_GUIDE.md) — operational workflow
