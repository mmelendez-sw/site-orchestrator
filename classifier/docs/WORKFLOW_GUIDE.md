# Asset Classifier — How It Works (Plain English)

*Module 2 of a three-part sales intelligence workflow*

---

## Where this fits in the bigger picture

You are building three connected steps. This codebase is **Step 2**.

| Step | Purpose | Status |
|------|---------|--------|
| **1 — Site discovery** | Find *new potential* cell sites inside zip codes or cities | Planned / upstream |
| **2 — Imagery classification** *(this tool)* | Look at aerial photos of each coordinate and answer: **tower or rooftop?** Is **cell equipment visible?** Where is the asset? | **Built & running (pilot)** |
| **3 — Ownership lookup** | Find who owns the land parcel or building roof (open data or paid API) | Planned / downstream |

**Deduplication (all steps):** Every module should eventually check candidates against your **Salesforce** records (live API pull or a staging **MSSQL** mirror — still TBD). Step 2 does not do that yet, but its output (`results.csv`) is shaped so you can join it to Salesforce by `id`, `lat`/`lon`, or a future parcel key.

---

## What you put in

A simple spreadsheet: **`assets.csv`**

Each row is one location you want evaluated:

- `id` — your reference name (e.g. `asset_007`)
- `lat`, `lon` — GPS point (the “address” may be approximate)
- `label` — optional tag (`JP`, `W/C`, `stealth`, etc.) that can steer the AI (e.g. stealth sites get extra scrutiny)

---

## What you get out

| Output | What it is |
|--------|------------|
| **`results.csv`** | One row per asset: site type, confidence scores, equipment yes/no, located coordinates, which imagery was used, errors |
| **`chips/`** | Saved photos the AI actually looked at — for human spot-checking |
| **`EXECUTIVE_SUMMARY.md`** | Auto-generated stakeholder summary after each run |

Key columns in `results.csv` for operations:

- `site_type` — `tower`, `rooftop`, `other`, `unclear`, or `no_imagery`
- `site_confidence` — 0–1 how sure the AI is about site type
- `cell_equipment` — `true` / `false` / blank if unknown
- `cell_equipment_confidence` — 0–1 for equipment visibility
- `nearmap_views` — which Nearmap angles were available (blank = NAIP only)
- `classification_stage` — which pass produced the answer: `primary`, `wide_aoi`, or `zoom`
- `asset_lat`, `asset_lon`, `asset_offset_m` — refined location when the box was drawn on NAIP
- `error` — if the row failed (e.g. AI overload), retry later

---

## The sequence of events (one asset at a time)

Think of it as an assembly line. For **each row** in `assets.csv`:

```
START
  │
  ├─ Already in results.csv with a good answer? ──YES──► SKIP (resume)
  │
  NO
  │
  ├─ 1. PULL NAIP (always, free public imagery)
  │     • ~250 m × 250 m square around the point
  │     • ~1 m resolution — good for context, weak on tiny antennas
  │     • Saved as: chips/{id}_NAIP.jpg
  │
  ├─ 2. PULL NEARMAP (if API key is set)
  │     • Tries high-res top-down + four 45° “side” views
  │     • Only where your subscription has coverage
  │     • Saved as: chips/{id}_nearmap_vert.jpg, _north, _east, etc.
  │
  ├─ 3. ENOUGH IMAGERY?
  │     • If BOTH NAIP and Nearmap are empty → mark no_imagery, STOP this asset
  │
  ├─ 4. PRIMARY AI REVIEW (Google Gemini)
  │     • Sends all available photos + instructions
  │     • Answers: tower vs rooftop vs other, equipment visible?, draw a box
  │     • stage = primary
  │
  ├─ 5. WIDE NEARMAP RETRY? (conditional)
  │     • IF: still “other” or “unclear”
  │     • AND: no oblique views (typical rural case)
  │     • THEN: re-fetch Nearmap over 250 m (same width as NAIP) and ask AI again
  │     • stage = wide_aoi
  │
  ├─ 6. ZOOM RETRY? (conditional)
  │     • IF: still “other” or “unclear”
  │     • OR: label is “stealth” (always gets a zoom pass)
  │     • THEN: magnify suspicious areas of the best top-down photo and ask AI again
  │     • Saved as: chips/{id}_zoom_1.jpg, _zoom_2.jpg, …
  │     • stage = zoom
  │
  ├─ 7. LOCATION MATH
  │     • If the AI drew a box on the NAIP image, convert it to lat/lon + meters off input point
  │
  ├─ 8. SAVE ROW to results.csv (after every asset — crash-safe)
  │
  └─ Wait ~12 seconds (rate limit), next asset
END RUN → refresh EXECUTIVE_SUMMARY.md
```

---

## What happens when Nearmap is missing?

This is **not a failure** — it is a **coverage branch**. The pipeline keeps going on NAIP alone.

| Situation | What the code does | What you see in results |
|-----------|-------------------|-------------------------|
| **Nearmap has everything** (urban/suburban) | 6 views: NAIP + vertical + 4 obliques | `nearmap_views`: `Vert,North,East,South,West` |
| **Nearmap vertical only** (many rural areas) | NAIP + 1 high-res top-down | `nearmap_views`: `Vert` |
| **No Nearmap at this coordinate** | NAIP only; may trigger zoom stage | `nearmap_views`: *(empty)* |
| **No NAIP either** | Row marked `no_imagery` | `site_type`: `no_imagery` |

**Example:** `asset_007` (eastern Oregon) — Nearmap returns zero surveys and 404 tiles. The run correctly used **NAIP + zoom only**. That is expected behavior, not a bug.

### Recommended manual flag (future column)

For operations and Salesforce sync, add a derived flag such as:

| Flag | Meaning | Suggested action |
|------|---------|------------------|
| `imagery_tier = full` | Nearmap vert + obliques | Trust AI more; spot-check low confidence only |
| `imagery_tier = nearmap_vert_only` | High-res top-down, no obliques | Review tower calls; obliques would help |
| `imagery_tier = naip_only` | No Nearmap coverage | **Manual review queue** — AI confidence capped; human or alternate imagery |
| `imagery_tier = none` | No imagery | Revisit coordinates or different source |

Today you can infer this from `nearmap_views`: blank = `naip_only`.

---

## Retry & recovery behavior

| Scenario | What happens | What you do |
|----------|--------------|-------------|
| **Script stopped mid-run** | Re-run the same command | Automatically **resumes** — skips rows already in `results.csv` without errors |
| **One asset errored** (e.g. AI “503 overloaded”) | Row saved with `error` filled | Delete that row from `results.csv` (or fix error), re-run — only that asset retries |
| **Wrong answer, want re-classify** | Remove that asset’s row from `results.csv` | Re-run; optional: delete its `chips/` files for a clean slate |
| **Gemini daily quota exhausted** | Script stops with message; partial `results.csv` saved | Wait for quota reset (midnight Pacific) or enable billing; re-run resumes |
| **Nearmap fetch throws** | Logged; asset continues on NAIP | No action unless every asset fails — then check API key |
| **Nearmap 404 (no tile)** | Treated as “no coverage”; not retried | Flag as `naip_only`; manual review or alternate imagery provider |

The pipeline **never loses finished work**: `results.csv` is rewritten after each asset.

---

## How the AI makes its calls (non-technical)

For each location the model is asked three questions:

1. **What kind of site is this?**
   - **Tower** — ground-based mast, monopole, lattice, stealth building with a tall tower section
   - **Rooftop** — equipment on a building roof
   - **Other** — something else (farm, empty lot, wrong structure)
   - **Unclear** — can’t tell with available photos

2. **Where is the asset?**
   - Draws a box on the clearest image
   - On NAIP, that box is converted to map coordinates and “meters from input point”

3. **Is cellular equipment visible?**
   - Antennas, sector panels, dishes, ground cabinets, etc.
   - Separate confidence score from site-type confidence

**Important limitation:** The AI only sees what the photos show. At ~1 m NAIP resolution, small rooftop gear and thin towers are easy to miss. Nearmap obliques (45°) are the biggest accuracy boost — when they exist.

---

## Imagery sources in plain terms

| Source | Cost | Resolution | View | Best for |
|--------|------|------------|------|----------|
| **NAIP** | Free (public) | ~1 m | Straight down | Wide context; off-center towers; entire US |
| **Nearmap vertical** | Subscription GB | ~7 cm | Straight down | Roof detail, shadows, small compounds |
| **Nearmap oblique** | Same subscription | ~7 cm | 45° from N/E/S/W | **Antennas, monopines, stealth towers** |
| **Zoom crops** | Free (derived) | Magnified NAIP/Nearmap | Straight down | Rural / stealth when wide view fails |

Your subscription uses the **Tile API** (monthly GB allowance). The Transactional Content API is **not** enabled on your plan (those calls return 403) — the code uses Tile API instead.

---

## Pilot results snapshot (6 + 1 assets)

| Asset | Nearmap | Result | Notes |
|-------|---------|--------|-------|
| 001 | Full (6 views) | Rooftop, 95%, equipment yes | Obliques showed roof antennas |
| 002 | Full | Rooftop, 30%, equipment no | Low confidence — review chips |
| 003 | Vert only | Tower, 99%, equipment yes | Shadow + compound; 60 m off coordinate |
| 004 | Full | Tower, 95%, equipment yes | Monopine found via obliques |
| 005 | Vert only | Other, 80% | Missed lattice tower — zoom/manual candidate |
| 006 | Full | Tower, 90%, equipment no | Model thought transmission tower |
| 007 | **None** | Unclear, 30% | **NAIP only** — stealth tower visible to human review |

---

## Hooks for your upcoming modules

### Module 1 → Module 2 (discovery → classify)

- Output of Step 1 should append rows to `assets.csv` (or a shared queue table)
- Carry through: `source_zip`, `source_city`, `discovery_reason`, `salesforce_checked` (Y/N)
- **Dedup:** Before inserting, query Salesforce / MSSQL for existing accounts or coordinates within X meters

### Module 2 → Module 3 (classify → ownership)

Pass forward when `site_type` is `tower` or `rooftop` and confidence exceeds your threshold:

- `asset_lat`, `asset_lon` (refined if available)
- `site_type`, `cell_equipment`
- `imagery_tier` (for confidence weighting in downstream)

### Module 2 → Salesforce dedup

Suggested match keys (implement in Module 1 or a shared dedup service):

- Exact or fuzzy match on existing **Account** coordinates
- Radius search (e.g. 50 m) against known sites
- Store `sf_duplicate_of`, `sf_status` on each result row

---

## Decision cheat sheet (for operators)

```
Nearmap views empty?
  YES → NAIP-only path → expect lower confidence → manual review queue
  NO, only "Vert" → rural path → wide AOI may run → zoom if still unclear
  NO, full set → best path → trust unless confidence < 60%

site_type = unclear or other?
  → Check chips/ folder
  → Consider manual override or re-run after prompt tweaks

error column filled?
  → Delete row, re-run (resume skips good rows)

label = stealth?
  → Always gets zoom pass
  → Human review recommended even on "rooftop" calls
```

---

## Files to edit when you extend the system

| File | Role |
|------|------|
| `assets.csv` | Input queue from Module 1 |
| `asset_classifier.py` | Core pipeline — imagery fetch, AI, fallbacks |
| `results.csv` | Output for Module 3 + Salesforce |
| `chips/` | Audit trail / training data for prompt improvements |
| `.env` | `GEMINI_API_KEY`, `NEARMAP_API_KEY` |

---

*This document describes the pipeline as of the pilot run. Update it when you add Salesforce dedup, MSSQL staging, manual flags, or Module 3 ownership integration.*
