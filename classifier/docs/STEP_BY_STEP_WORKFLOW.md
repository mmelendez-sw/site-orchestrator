# Site Classifier — Step-by-Step Workflow & External Tools

This document describes **exactly** what happens when you run `python asset_classifier.py`, and what **Gemini**, **Nearmap**, and **Claude** each do in (or around) this project.

---

## Big picture

This repo is **Module 2** of a three-step sales-intelligence workflow:

| Step | Purpose |
|------|---------|
| 1 — Site discovery | Find candidate cell sites in zip codes / cities (upstream, planned) |
| **2 — Imagery classification** *(this tool)* | Look at aerial photos and answer: tower or rooftop? Is cell equipment visible? Where is the asset? |
| 3 — Ownership lookup | Find who owns the land or building (downstream, planned) |

**Input:** `assets.csv` — one row per location (`id`, `lat`/`lon` or `address`, optional `label`, `input_confidence`)

**Output:** `results.csv`, saved images in `chips/`, and `EXECUTIVE_SUMMARY.md`

---

## External tools at a glance

| Tool | Used at runtime? | Role |
|------|------------------|------|
| **Nearmap** | Yes (optional) | Fetches high-resolution aerial imagery (top-down + 45° obliques) |
| **Gemini** | Yes (required) | All AI vision: classify site type, locate asset, detect equipment, scout zoom regions |
| **Claude** | **No** | Not called by the pipeline. Used only as the *intended audience* for `PROMPT_FOR_ORCHESTRATOR.md` when designing the broader multi-module orchestrator |

> **Note:** The README line “spot-check Claude's calls” is outdated — the classifier uses **Gemini**, not Claude.

---

## What Nearmap is used for

**Provider:** Nearmap Tile API (`api.nearmap.com`)  
**Env var:** `NEARMAP_API_KEY`  
**Cost model:** Subscription GB allowance (Tile API), not transactional credits

Nearmap is an **imagery source only**. It does not classify anything. The code:

1. Calls the **Coverage API** to get capture date metadata (informational).
2. For each view — `Vert`, `North`, `East`, `South`, `West` — downloads map tiles and stitches them into one image per view.
3. Saves images to `chips/{id}_nearmap_vert.jpg`, `_north.jpg`, `_east.jpg`, etc.
4. Passes those images to **Gemini** for classification.

| View | What it is | Why it matters |
|------|------------|----------------|
| **Vert** | ~7 cm straight-down | Roof detail, shadows, small compounds |
| **North/East/South/West** | 45° oblique panoramas | Antennas, monopines, stealth towers — sides of structures visible |

**Parameters:**
- Initial AOI: **100 m × 100 m** (`NEARMAP_CHIP_M`)
- Wide fallback AOI: **250 m × 250 m** (`NEARMAP_FALLBACK_CHIP_M`) — same width as NAIP

**If Nearmap is missing or fails:** The pipeline continues on NAIP only. Empty `nearmap_views` in results = no Nearmap coverage at that coordinate.

---

## What Gemini is used for

**Provider:** Google Gemini via `google-genai` SDK  
**Env var:** `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)  
**Models (fallback chain):** `gemini-2.5-flash` → `gemini-2.5-flash-lite` → `gemini-3.5-flash` → `gemini-3-flash-preview` → `gemini-2.0-flash-lite`

Gemini is the **only AI** used at runtime. Every vision call goes through `_call_gemini_json()`. Gemini receives labeled JPEG images plus a text prompt and must return structured JSON.

### Gemini call #1 — Primary classification (`classification_stage = primary`)

**When:** After NAIP + Nearmap images are assembled  
**Function:** `classify_chip()`  
**Prompt:** `CLASSIFICATION_PROMPT` (+ optional stealth label hint + source-trust hint)

**Questions Gemini answers:**
1. **Site type:** `tower` | `rooftop` | `other` | `unclear`
2. **Asset location:** bounding box `[ymin, xmin, ymax, xmax]` in 0–1000 normalized coords + which view the box was drawn on
3. **Cell equipment visible?** `true` | `false` | `null` + confidence and evidence

**Input images:** All available views — typically NAIP top-down plus 0–5 Nearmap views.

---

### Gemini call #2 — Equipment recheck (conditional, same stage)

**When:** `input_confidence` is `high` or `medium`, primary pass returned `cell_equipment = false`, and at least 2 views exist  
**Function:** `maybe_recheck_equipment()`  
**Prompt:** `EQUIPMENT_RECHECK_PROMPT`

**Purpose:** Trusted sources expect gear at the site; re-examine obliques and shaded roof areas before accepting “no equipment.”

---

### Gemini call #3 — Primary classification again (`classification_stage = wide_aoi`)

**When:** Primary result is `other` or `unclear`, Nearmap key is set, and **no oblique views** were returned (typical rural vert-only case)  
**Function:** `classify_chip()` again after re-fetching Nearmap at 250 m AOI

**Purpose:** The real tower may sit outside the narrow 100 m Nearmap window.

---

### Gemini call #4 — Scout candidates (`classification_stage = zoom`, step A)

**When:** Still `other`/`unclear` after wide AOI, **or** `label = stealth` (stealth always gets zoom)  
**Function:** `scout_candidates()`  
**Prompt:** `SCAN_PROMPT`

**Purpose:** On the best top-down image (Nearmap Vert if available, else NAIP), propose up to 4 suspicious regions (tower shadows, lattice patterns, rooftop mounts). Falls back to a 3×3 grid if scout finds nothing.

---

### Gemini call #5 — Zoom re-classification (`classification_stage = zoom`, step B)

**When:** Immediately after scout; zoom crops are built and magnified  
**Function:** `run_zoom_stage()` → `classify_chip()`  
**Prompt:** `ZOOM_CLASSIFICATION_PROMPT`

**Input images:** One wide context view + up to 6 magnified zoom crops (`chips/{id}_zoom_1.jpg`, …)

**Purpose:** Second pass on magnified regions where wide imagery failed.

---

### Gemini rate limits & resilience

- **~12 second delay** between assets (free tier ~10 req/min)
- Retries on transient **429 / 503**
- On daily quota exhaustion for one model, **hops to the next model** in the chain
- If all models exhausted, script exits with partial `results.csv` saved

---

## What Claude is used for

**Claude is not integrated into `asset_classifier.py`.** There is no Anthropic API key, no Claude SDK, and no runtime calls to Claude.

Claude appears in this repo in two places:

1. **`PROMPT_FOR_ORCHESTRATOR.md`** — A copy-paste prompt meant for Claude (or another LLM) to **design** the broader three-module orchestrator: repo layout, data flow, retry policy, Salesforce dedup hooks, etc. That is **planning/documentation**, not execution.

2. **`README.md`** — Says “spot-check Claude's calls” when reviewing `chips/`; that should read **Gemini's calls**. It is a documentation typo.

If you want Claude in the pipeline later, it would be a separate integration (e.g. as an alternate classifier or for orchestration logic in Module 1/3).

---

## Step-by-step: one full run

### Phase 0 — Startup

```
1. Parse CLI args (input CSV, optional run folder, report paths)
2. Create timestamped run folder under runs/ (or resume existing)
3. Copy input CSV into run folder
4. Load GEMINI_API_KEY — exit if missing
5. Read assets.csv; validate each row has id + (lat/lon OR address)
6. Load existing results.csv if present → build done_ids set for resume
```

---

### Phase 1 — Per asset (loop)

For **each row** in `assets.csv` not already successfully classified:

#### Step 1 — Skip if already done

```
IF row id is in results.csv with site_type set AND no error
  → SKIP (resume mode)
```

#### Step 2 — Resolve coordinates

```
IF row has lat + lon
  → use them directly
ELSE IF row has address
  → geocode via US Census Geocoder (CONUS)
  → fallback: OpenStreetMap Nominatim
  → write geocode_source, geocode_matched_address to result
```

**External services:** Census API, Nominatim — not Gemini/Nearmap/Claude.

#### Step 3 — Fetch NAIP imagery (always attempted)

```
Query Microsoft Planetary Computer STAC for newest NAIP scene at point
Window-read 250 m × 250 m chip (~1 m resolution) from Cloud-Optimized GeoTIFF
Save: chips/{id}_NAIP.jpg
Store geo bounds for later box → lat/lon conversion
```

**External service:** Microsoft Planetary Computer (free public NAIP) — not Gemini/Nearmap/Claude.

#### Step 4 — Fetch Nearmap imagery (optional)

```
IF NEARMAP_API_KEY is set
  FOR each view in [Vert, North, East, South, West]
    Download tiles at zoom 21 (Vert) or 20 (obliques)
    Stitch into one image per view
    Skip views with 404 (no coverage)
  Save chips/{id}_nearmap_*.jpg
  Fetch capture date from Coverage API
ELSE
  nearmap_views = empty
```

**External service:** Nearmap Tile API + Coverage API.

#### Step 5 — Check imagery availability

```
IF no NAIP AND no Nearmap
  → site_type = no_imagery, save row, wait 12s, next asset
```

#### Step 6 — Primary Gemini classification

```
Build view list: [("NAIP top-down", img), ("Nearmap top-down", ...), ("Nearmap oblique (N)", ...)]
Append label hints (stealth) and input_confidence trust text to prompt
→ GEMINI: classify_chip() → JSON result
→ GEMINI (maybe): maybe_recheck_equipment() if trusted source said false
classification_stage = "primary"
```

#### Step 7 — Wide Nearmap retry (conditional)

```
IF result is other/unclear
AND NEARMAP_API_KEY set
AND no oblique views were fetched
  → NEARMAP: re-fetch at 250 m AOI
  → GEMINI: classify_chip() again
  → GEMINI (maybe): equipment recheck
  classification_stage = "wide_aoi"
```

#### Step 8 — Zoom retry (conditional)

```
IF result is other/unclear OR label == "stealth"
  Pick best top-down source (Nearmap Vert > NAIP)
  → GEMINI: scout_candidates() — find suspicious regions
  Crop and magnify regions → chips/{id}_zoom_*.jpg
  → GEMINI: classify_chip() on zoom crops
  IF zoom result is better (tower/rooftop, higher confidence, or stealth)
    → adopt zoom result
    classification_stage = "zoom"
```

#### Step 9 — Convert bounding box to coordinates

```
IF Gemini drew box on "NAIP top-down" view
  → math: normalized box → projected coords → WGS84 lat/lon
  → compute asset_offset_m from input point
ELSE
  → asset_lat/lon stay null (box may be on Nearmap view only)
```

**No external API** — pure geometry using NAIP georeferencing.

#### Step 10 — Save result row

```
Merge classification fields into record
Pick best review image path (oblique > vert > NAIP > zoom)
Append to results list
Rewrite results.csv immediately (crash-safe)
Sleep 12 seconds (Gemini rate limit)
```

---

### Phase 2 — After all assets

```
1. Write EXECUTIVE_SUMMARY.md (stakeholder summary table)
2. Write stakeholder CSV + Excel with embedded review photos (if configured)
3. Print run complete summary
```

**No external API** — local file generation only.

---

## Decision flow (ASCII)

```
START asset
  │
  ├─ already in results.csv? ──YES──► SKIP
  │
  NO
  │
  ├─ geocode address (if needed)
  │
  ├─ NAIP fetch ─────────────────────────────► free public imagery
  │
  ├─ Nearmap fetch (if key set) ─────────────► subscription imagery
  │
  ├─ no imagery at all? ──YES──► no_imagery, SAVE, NEXT
  │
  ├─ GEMINI primary classify
  ├─ GEMINI equipment recheck? (conditional)
  │
  ├─ still other/unclear + vert-only Nearmap?
  │     └─► Nearmap wide AOI + GEMINI re-classify
  │
  ├─ still other/unclear OR stealth label?
  │     └─► GEMINI scout + zoom crops + GEMINI re-classify
  │
  ├─ box on NAIP? → lat/lon math
  │
  └─ SAVE row → wait 12s → NEXT asset

END RUN → EXECUTIVE_SUMMARY.md
```

---

## Who does what — quick reference

| Task | Who |
|------|-----|
| High-res top-down + oblique photos | **Nearmap** |
| Site type (tower/rooftop/other/unclear) | **Gemini** |
| Draw asset bounding box | **Gemini** |
| Detect visible cell equipment | **Gemini** |
| Find zoom candidate regions | **Gemini** |
| Re-classify on zoomed crops | **Gemini** |
| Re-check equipment in shadow/obliques | **Gemini** |
| Wide-area context (~1 m, 250 m) | NAIP (free, not one of the three) |
| Geocode street addresses | Census / Nominatim (free) |
| Design multi-module orchestrator | **Claude** *(manual prompt only, not runtime)* |
| Classify imagery at runtime | **Not Claude** |

---

## Output columns tied to external tools

| Column | Source |
|--------|--------|
| `nearmap_views` | Nearmap — e.g. `Vert,North,East,South,West` or empty |
| `nearmap_date` | Nearmap Coverage API |
| `nearmap_aoi_m` | 100 (narrow) or 250 (wide fallback) |
| `site_type`, `site_confidence`, `site_evidence` | Gemini |
| `cell_equipment`, `cell_equipment_confidence` | Gemini |
| `asset_box_2d`, `asset_view` | Gemini |
| `asset_lat`, `asset_lon`, `asset_offset_m` | Gemini box + NAIP georeferencing |
| `classification_stage` | Which Gemini pass won: `primary`, `wide_aoi`, `zoom` |
| `model` | Which Gemini model answered |
| `zoom_crops` | Count of zoom images sent to Gemini |
| `error` | Any failure (Gemini 503, geocode miss, etc.) |

---

## Files to know

| File | Purpose |
|------|---------|
| `assets.csv` | Input queue |
| `asset_classifier.py` | Full pipeline |
| `.env` | `GEMINI_API_KEY`, `NEARMAP_API_KEY` |
| `results.csv` / `*_results_detail.csv` | Per-asset output |
| `chips/` | Saved imagery for human audit |
| `EXECUTIVE_SUMMARY.md` | Auto-generated run summary |
| `WORKFLOW_GUIDE.md` | Operator-focused plain-English guide |
| `PROMPT_FOR_ORCHESTRATOR.md` | Prompt for **Claude** to design the broader system |

---

*Generated from `asset_classifier.py` as of pilot run. Update when integrations change.*
