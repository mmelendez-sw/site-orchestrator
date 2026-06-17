# Asset Classifier — Executive Summary

*Generated: June 11, 2026 at 03:40 PM*

## At a glance

We built an automated pipeline that takes a list of coordinates and determines whether each location is a **tower site** or **rooftop cellular site**, whether **cellular equipment is visible**, and where the asset sits relative to the recorded point.

- **Assets evaluated:** 7
- **Successfully classified:** 7
- **Tower sites identified:** 3
- **Rooftop sites identified:** 2
- **Cellular equipment detected:** 3
- **Assets with a located position:** 6
- **Errors:** 0

## Pilot run highlights

This proof-of-concept run combined **free public NAIP imagery**, **Nearmap high-resolution + 45° oblique views**, and **Google Gemini vision AI** to classify six sample assets (2 urban NJ, 4 rural W/C).

**What worked well:**

- **Rooftop detection (urban):** Nearmap obliques revealed rooftop antenna sectors on a classical building (asset_001, 95% confidence)
- **Tower detection (rural):** Monopoles identified from shadow signatures even when 60 m from the recorded coordinate (asset_003)
- **Disguised towers:** A monopine (tower disguised as a pine tree) was correctly identified only from oblique imagery (asset_004)
- **Off-center assets:** The pipeline searches the full image, not just the center — critical when coordinates are imprecise

**Improvement in progress:**

- asset_005 (rural Oregon) was missed in wide imagery but a human reviewer confirmed a lattice tower in the top-right of the NAIP chip. A **two-stage zoom** pass (now implemented) magnifies suspicious regions before re-classifying — designed specifically for this case.

## How it works

```
Coordinates (CSV)
       |
       v
  +----+----+
  |  NAIP   |  Wide public aerial (~250 m, ~1 m resolution)
  +----+----+
       |
       v
  +----+----+
  | Nearmap |  High-res top-down + 45-degree obliques (urban/suburban)
  +----+----+
       |
       v
  +----+----+
  | Gemini  |  AI vision: classify site, locate asset, detect equipment
  +----+----+
       |
       v (if rural / still unidentified)
  +----+----+
  |  Zoom   |  Magnify suspicious regions and re-classify
  +----+----+
       |
       v
  results.csv + review chips + this summary
```

### Imagery sources

| Source | What it provides | Why it matters |
|---|---|---|
| **NAIP** (free, public) | Wide top-down context around each point | Catches off-center towers; cheap baseline for the full US |
| **Nearmap** (subscription) | ~7 cm top-down + 45° oblique views | Makes rooftop antennas and disguised towers (e.g. monopines) visible |
| **Gemini** (Google AI) | Structured classification from multi-image input | Turns imagery into site type, equipment call, and location |

### Confidence safeguards

1. **Whole-image search** — never assumes the asset is at the exact center
2. **Multi-view fusion** — NAIP context + Nearmap detail + oblique angles
3. **Rural fallback** — widens Nearmap area when only vertical imagery exists
4. **Two-stage zoom** — magnifies subtle structures the wide view missed
5. **Human review chips** — every image sent to the model is saved for audit

## Results by asset

| Asset | Region | Site type | Confidence | Cell equip. | Located | Method | Key finding |
|---|---|---|---:|---|---|---|---|
| asset_001 | JP | rooftop | 95% | Yes | 16 m off | nan | The tall white classical building in the center of the Nearmap oblique views displays roof… |
| asset_002 | JP | rooftop | 30% | No | 3 m off | nan | The imagery shows a multi-story office building, but no cell tower is present in the vicin… |
| asset_003 | W/C | tower | 99% | Yes | 60 m off | nan | A monopole tower with a distinct long shadow and fenced compound is visible in both NAIP a… |
| asset_004 | W/C | tower | 95% | Yes | 17 m off | nan | A monopole styled as a pine tree (monopine) is visible next to the lawn circular feature i… |
| asset_006 | W/C | tower | 90% | No | yes | nan | A tower is clearly visible in the Nearmap top-down and oblique views, consistent with the … |
| asset_005 | W/C | other | 80% | — | yes | wide AOI | The imagery shows a residential area with fields and houses, but no clear tower or rooftop… |
| asset_007 | stealth | unclear | 30% | Unknown | — | zoom | The zoom crops show a variety of buildings and vehicles but do not clearly depict any cell… |

## Operational notes

- **Nearmap data usage (pilot run):** ~15 MB for 7 assets (under 1% of the 2.49 GB/month subscription allowance)
- **Review folder:** saved images in `chips/` — NAIP chips named `*_NAIP.jpg`, Nearmap views `*_nearmap_*.jpg`, zoom crops `*_zoom_*.jpg`
- **Machine-readable output:** `results.csv` for downstream systems

## Known limitations

- Rural sites may have Nearmap vertical imagery only (no 45° obliques)
- Very small lattice towers can still be missed until the zoom stage runs
- Recorded coordinates can be tens of meters off the true asset
- AI calls should be spot-checked on low-confidence results (< 60%)

## Recommended next steps

1. Spot-check chips for any low-confidence or unexpected classifications
2. Scale to the full asset list once stakeholders approve the approach
3. Feed confirmed results back into the coordinate/enrichment workflow
