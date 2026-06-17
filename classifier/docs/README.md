# Asset Classifier — lat/lon → aerial chip → Claude classification

Takes a CSV of coordinates or addresses, pulls the newest public-domain NAIP aerial image chip for each point, and asks Claude to classify the central asset as tower / rooftop / other. Outputs detail CSVs plus saved chips for review.

> This classifier is one stage of the **site-orchestrator** pipeline. See the [project README](../../README.md) for source discovery, dedupe, and Salesforce loading.

## Project files

```
classifier/
├── asset_classifier.py   # the pipeline
└── docs/                 # configuration and workflow guides

requirements.txt          # at repo root
data/assets.csv           # sample input CSV
```

## Setup (one time)

1. Open the repo root in Cursor and create a virtual environment:

   ```bash
   python -m venv .venv
   # macOS / Linux:
   source .venv/bin/activate
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1

   pip install -r requirements.txt
   ```

2. Set your Anthropic API key (create one at https://console.anthropic.com/):

   ```bash
   # macOS / Linux:
   export ANTHROPIC_API_KEY=sk-ant-your-key-here
   # Windows (PowerShell):
   $env:ANTHROPIC_API_KEY="sk-ant-your-key-here"
   ```

   Or add `ANTHROPIC_API_KEY=sk-ant-...` to a local `.env` file at the repo root.

   Default model: `claude-sonnet-4-6`, with `claude-haiku-4-5-20251001`
   as fallback. Override with `CLAUDE_MODELS=model-a,model-b` (comma-separated).

3. (Optional) Nearmap oblique imagery. If you have a Nearmap subscription, set:

   ```bash
   $env:NEARMAP_API_KEY="your-nearmap-key"
   ```

   Each asset then also gets a high-res top-down view plus 45° oblique panoramas
   from four compass directions. Without the key the pipeline runs NAIP-only.

## Run

1. Edit `data/assets.csv` — keep the header row, one asset per line. Each row needs
   `id` plus **either** decimal `lat`/`lon` (WGS84) **or** a full street
   `address` (geocoded automatically before imagery is fetched):

   ```
   id,lat,lon,label,input_confidence
   site_0001,40.689247,-74.044502,JP,high

   id,address,label,input_confidence
   site_0002,"350 5th Ave, New York, NY 10118",JP,high
   ```

   Address geocoding uses the free **US Census Geocoder** (best for CONUS
   rooftop addresses) and falls back to **OpenStreetMap Nominatim**. Set
   `GEOCODER=nominatim` to skip Census.

2. From the repo root:

   ```bash
   python classifier/asset_classifier.py -i data/assets.csv
   ```

3. Outputs land in a timestamped folder under `runs/` (detail CSV, stakeholder
   report, executive summary, and chips for spot-checking).

See [`CONFIGURATION.md`](CONFIGURATION.md) for pipeline flags (`NAIP_ONLY`,
`NEARMAP_TIERED`, `BIFURCATED_AI`) and [`WORKFLOW_GUIDE.md`](WORKFLOW_GUIDE.md)
for batch run guidance.
