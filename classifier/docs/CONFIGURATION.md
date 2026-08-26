# Asset Classifier — Configuration Reference

All runtime settings are read from **environment variables**. The pipeline calls
`load_dotenv()` at startup (see `asset_classifier.py`), which loads a local
`.env` file into the process environment if it exists.

## How settings are loaded

1. **`.env` file** (project root) — loaded automatically via `python-dotenv`.
2. **Shell variables** — set before running (`$env:VAR="..."` in PowerShell,
   `export VAR=...` in bash). Shell values **override** `.env` if both are set.
3. **Defaults in code** — used when a variable is not set anywhere.

Your current `.env` only defines API keys (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`NEARMAP_API_KEY`). Feature flags and tuning knobs use their code defaults unless
you add them to `.env` or set them in the shell.

Copy `.env.example` to `.env` and fill in values as needed.

---

## API keys

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Always | — | Claude API key. Required for every run. |
| `GEMINI_API_KEY` | When `BIFURCATED_AI=1` | — | Gemini API key for first-pass classification. |
| `NEARMAP_API_KEY` | Optional | — | Nearmap Tile API key. Without it, pipeline runs NAIP-only (Nearmap fetch skipped). |

---

## Pipeline mode flags

Boolean flags: set to `1`, `true`, or `yes` to enable. Default is `0` (off).

| Variable | Default | When enabled |
|---|---|---|
| `NEARMAP_TIERED` | `0` | Tiered Nearmap fetch: NAIP → Vert → obliques. Stops early when classification is confident. |
| `BIFURCATED_AI` | `0` | Gemini first pass; escalate to Claude for `other`, `unclear`, or low confidence. |
| `GEMINI_ONLY` | `0` | Gemini only — no Claude calls or escalation. Requires `GEMINI_API_KEY`. |
| `TOWER_ONLY` | `0` | Tower detection mode: rooftop hosts classify as `other`; prompts/schemas focus on ground-based towers. |
| `ZOOM_STAGE` | `1` | Two-stage zoom scout + re-classify on `other`/`unclear`. Set `0` to skip (saves API calls). |
| `WIDE_AOI_STAGE` | `1` | On `other`/`unclear`, refetch a wider NAIP chip (`NAIP_WIDE_CHIP_M`) and re-classify (zoom-out). One extra Gemini call. |
| `NAIP_ONLY` | `0` | Debug/breakpoint mode: skip all Nearmap fetching. Overrides `NEARMAP_TIERED`. |

**Combinations**

| `NAIP_ONLY` | `NEARMAP_TIERED` | `BIFURCATED_AI` | `GEMINI_ONLY` | `TOWER_ONLY` | Behavior |
|---|---|---|---|---|---|
| `0` | `0` | `0` | `0` | `0` | Legacy: all 5 Nearmap views upfront, Claude only |
| `0` | `1` | `0` | `0` | `0` | Tiered Nearmap, Claude only |
| `0` | `0` | `1` | `0` | `0` | Full Nearmap upfront, Gemini → Claude escalation |
| `0` | `1` | `1` | `0` | `0` | Tiered Nearmap on Gemini; Claude gets imagery already fetched if escalated |
| `1` | * | * | `1` | * | NAIP imagery only, Gemini only (no Claude) |
| `1` | * | * | `1` | `1` | NAIP + tower-only + Gemini only (typical 150-site pilot) |

---

## AI model settings

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_MODELS` | `claude-sonnet-4-6,claude-haiku-4-5-20251001` | Comma-separated Claude models. First is primary; hops to next on rate limits or 404. Used when `BIFURCATED_AI=0`, or for Claude escalation / wide-AOI / zoom when bifurcated. |
| `CLAUDE_ESCALATION_MODEL` | `claude-sonnet-4-6` | Fixed Claude model for bifurcated escalation (does not use fallback chain). |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model for first pass when `BIFURCATED_AI=1` or `GEMINI_ONLY=1`. |
| `GEMINI_DELAY_S` | `30` | Seconds between assets when Gemini is the primary provider. |
| `GEMINI_RETRIES` | `6` | Retries per Gemini call on transient `429` / `503` errors. |
| `GEMINI_RETRY_BASE_S` | `20` | Base backoff seconds for Gemini retries (doubles each attempt, cap 120s). |
| `NAIP_WIDE_CHIP_M` | `500` | Wide NAIP chip side length (meters) when `WIDE_AOI_STAGE=1`. |
| `CHIP_SIZE_M` | `250` | Primary NAIP chip side length. Set `500` to start wide (skip a prior 250 m pass). |
| `CLAUDE_DELAY_S` | `12` | Seconds to pause between assets when Claude is the primary provider. |

---

## Tiered Nearmap gating

Used when `NEARMAP_TIERED=1` to decide whether to stop fetching the next tier.

| Variable | Default | Description |
|---|---|---|
| `TIER_CONF_HIGH` | `0.75` | `site_confidence` ≥ this → **high** band |
| `TIER_CONF_MEDIUM` | `0.6` | `site_confidence` ≥ this (and < high) → **medium** band |

**Stop after Tier 0 or 1** when all are true:

- `site_type` is `tower` or `rooftop`
- Confidence band is `medium` or `high` (not `low`)
- `cell_equipment` is not null

---

## Geocoding

| Variable | Default | Description |
|---|---|---|
| `GEOCODER` | `auto` | `auto` (Census then Nominatim), `census`, or `nominatim` |
| `GEOCODER_USER_AGENT` | `site-classifier/1.0 (...)` | User-Agent for Nominatim requests |

---

## Hardcoded constants (edit in `asset_classifier.py`)

These are not environment variables; change them in code if needed.

| Constant | Default | Description |
|---|---|---|
| `CHIP_SIZE_M` | `250` | NAIP chip side length (meters) |
| `NEARMAP_CHIP_M` | `100` | Nearmap AOI side length (meters) |
| `NEARMAP_FALLBACK_CHIP_M` | `250` | Wide-AOI retry size (Tier 3 fallback) |
| `NEARMAP_VERT_ZOOM` | `21` | Nearmap vertical tile zoom |
| `NEARMAP_OBLIQUE_ZOOM` | `20` | Nearmap oblique tile zoom |
| `NEARMAP_MAX_PX` | `2048` | Max stitched Nearmap image dimension |
| `ZOOM_GRID` | `3` | Grid fallback when scout finds no candidates |
| `ZOOM_MAX_CANDIDATES` | `6` | Max zoom crops sent to classifier |
| `ZOOM_OUTPUT_PX` | `1024` | Magnified crop size (pixels) |
| `GEOCODE_DELAY_S` | `1.1` | Nominatim throttle (seconds) |

---

## CLI arguments

| Flag | Description |
|---|---|
| `-i`, `--input` | Input CSV (default: `assets.csv`) |
| `--run-dir` | Resume an existing run folder under `runs/` |
| `-o`, `--output` | Detail CSV filename inside run folder |
| `--report-csv` | Stakeholder summary CSV |
| `--report-xlsx` | Stakeholder Excel with photos |
| `--regenerate-report` | Rebuild reports from detail CSV (no API calls) |

---

## Output columns added by mode flags

| Column | When populated |
|---|---|
| `image_date` | Always when NAIP found: acquisition date from STAC (`datetime`) |
| `naip_year` / `naip_state` / `naip_gsd_m` | NAIP STAC photo metadata |
| `image_age_years` | Years since `image_date` (how stale the photo is) |
| `naip_chip_m` | Chip side length used for the winning classification (250 or wide) |
| `nearmap_tier` | Always (when run completes): `naip_only`, `naip_wide`, `vert_only`, `full`, `wide_aoi`, `zoom` |
| `primary_model` | Always: `gemini` or `claude` |
| `escalation_model` | When `BIFURCATED_AI=1` and escalation occurred |
| `escalation_reason` | `low_confidence`, `unclear_type`, `other_type`, `gemini_high_conf_tower` |

---

## Example `.env`

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Optional — Nearmap high-res + obliques
NEARMAP_API_KEY=your-nearmap-key

# Optional — only needed when BIFURCATED_AI=1
GEMINI_API_KEY=your-gemini-key

# Pipeline modes (all default to 0 if omitted)
# NEARMAP_TIERED=1
# BIFURCATED_AI=1
# GEMINI_ONLY=1
# TOWER_ONLY=1
# NAIP_ONLY=1

# Tuning (optional)
# CLAUDE_MODELS=claude-sonnet-4-6,claude-haiku-4-5-20251001
# CLAUDE_ESCALATION_MODEL=claude-sonnet-4-6
# GEMINI_MODEL=gemini-2.5-flash
# CLAUDE_DELAY_S=12
# TIER_CONF_HIGH=0.75
# TIER_CONF_MEDIUM=0.6
# GEOCODER=auto
```

## Example PowerShell one-liners

```powershell
# Fast local debug — NAIP only, Gemini tower screening, no Nearmap/Claude quota
$env:NAIP_ONLY="1"; $env:GEMINI_ONLY="1"; $env:TOWER_ONLY="1"; $env:ZOOM_STAGE="0"; $env:GEMINI_MODEL="gemini-3.1-flash-lite"; $env:GEMINI_DELAY_S="30"; python classifier/asset_classifier.py -i data/no_match_naip_150.csv

# Production-style tiered + bifurcated (set keys in .env first)
$env:NEARMAP_TIERED="1"; $env:BIFURCATED_AI="1"; python asset_classifier.py -i dc-assets.csv
```
