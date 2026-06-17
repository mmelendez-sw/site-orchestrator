# Asset Classifier — lat/lon → aerial chip → Claude classification

Takes a CSV of coordinates, pulls the newest public-domain NAIP aerial image
chip for each point, and asks Claude to classify the central asset as
tower / rooftop / other. Outputs `results.csv` plus the saved chips for review.

## Project files

```
asset_classifier.py   # the pipeline
requirements.txt      # python dependencies
assets.csv            # your input — replace the sample rows with real coordinates
```

## Setup (one time)

1. Put these three files in a folder and open that folder in Cursor.

2. Create a virtual environment and install dependencies. In Cursor's
   terminal (Terminal → New Terminal):

   ```bash
   python -m venv .venv
   # macOS / Linux:
   source .venv/bin/activate
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1

   pip install -r requirements.txt
   ```

3. Set your Anthropic API key (create one at https://console.anthropic.com/):

   ```bash
   # macOS / Linux:
   export ANTHROPIC_API_KEY=sk-ant-your-key-here
   # Windows (PowerShell):
   $env:ANTHROPIC_API_KEY="sk-ant-your-key-here"
   ```

   Or add `ANTHROPIC_API_KEY=sk-ant-...` to a local `.env` file.

   Default model: `claude-sonnet-4-6`, with `claude-haiku-4-5-20251001`
   as fallback. Override with `CLAUDE_MODELS=model-a,model-b` (comma-separated).

4. (Optional) Nearmap oblique imagery. If you have a Nearmap subscription, set:

   ```bash
   # Windows (PowerShell):
   $env:NEARMAP_API_KEY="your-nearmap-key"
   ```

   Each asset then also gets a ~7 cm top-down view plus 45° oblique panoramas
   from four compass directions, which make towers and rooftop antennas far
   easier to detect than top-down imagery alone. The Tile API bills against
   your subscription's monthly GB allowance. Without the key the pipeline
   runs NAIP-only.

## Run

1. Edit `assets.csv` — keep the header row, one asset per line. Each row needs
   `id` plus **either** decimal `lat`/`lon` (WGS84) **or** a full street
   `address` (geocoded automatically before imagery is fetched):

   ```
   id,lat,lon,label,input_confidence
   site_0001,40.689247,-74.044502,JP,high

   id,address,label,input_confidence
   site_0002,"350 5th Ave, New York, NY 10118",JP,high
   ```

   Address geocoding uses the free **US Census Geocoder** (best for CONUS
   rooftop addresses) and falls back to **OpenStreetMap Nominatim**. Resolved
   coordinates are written to `results.csv` along with `geocode_source` and
   `geocode_matched_address`. Set `GEOCODER=nominatim` to skip Census.

2. ```bash
   python asset_classifier.py
   ```

3. Results land in `results.csv` (classification, confidence, evidence,
   image date) and the cropped images in `chips/` so you can spot-check
   Claude's calls.

## Notes & knobs

See **[CONFIGURATION.md](CONFIGURATION.md)** for the full list of environment
variables, pipeline mode flags (`NEARMAP_TIERED`, `BIFURCATED_AI`, `NAIP_ONLY`),
and hardcoded constants. Settings load from a local `.env` file (via
`python-dotenv`) or from shell variables; copy `.env.example` to `.env` to start.

- `CHIP_SIZE_M` (default 250) — shrink to ~150 for tighter zoom if towers
  are being missed; grow for more context.
- `CLAUDE_MODELS` — comma-separated model list; first is primary, rest are
  fallbacks on rate limits or unavailability.
- `CLAUDE_DELAY_S` (default 12) — pause between assets to stay under API limits.
- NAIP covers the continental US only. For higher-fidelity state imagery
  (NY 6-inch, CT 3-inch, NJ 1-foot), the fetch stage can be swapped for an
  ArcGIS ImageServer or S3 adapter — the classification stage stays the same.
- First run on a new machine: `rasterio` wheels occasionally need a recent
  pip (`pip install --upgrade pip`) before installing.
