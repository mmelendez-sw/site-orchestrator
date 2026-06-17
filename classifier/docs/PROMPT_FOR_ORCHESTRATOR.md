# Prompt: Design datablender site-intelligence pipeline + orchestrator

Copy everything below the line into Claude (or another LLM) to generate architecture, repo layout, orchestrator code, and workflow specs.

---

## YOUR TASK

Design and scaffold a **three-stage data pipeline** for wireless site sales intelligence, with a **shared enrichment/dedup layer** (internally called **datablender**) and a **thin orchestrator** that can run stages independently or in sequence.

**Deliverables I need from you:**

1. **Architecture document** — components, data flow, retry semantics, failure branches (especially NAIP-only vs full Nearmap).
2. **Monorepo folder structure** — start as monorepo; show how to split into separate services later.
3. **`SiteRecord` schema** — canonical JSON/dataclass shared across all modules (Python `pydantic` preferred).
4. **Orchestrator** — CLI or lightweight job runner that supports:
   - **Path A:** CSV ingest (standalone Module 2, current behavior)
   - **Path B:** Module 1 → Module 2 → Module 3 pipeline
   - Per-stage resume, per-row retry, and stage-level skip
5. **Stub implementations** for Module 1 and Module 3 (interfaces + TODO), wrapping the **existing Module 2 code** without rewriting its core logic.
6. **`datablender-core` stub** — dedup interface against Salesforce (live API) and MSSQL mirror (TBD); normalize coordinates; derive `imagery_tier` and `manual_review_required`.
7. **Queue/handoff design** — how records move between stages (start with file-based handoff compatible with today's CSV; show migration path to DB/queue).
8. **Operator docs** — plain-English runbook matching the sequence below.

Do **not** assume any code exists beyond what is described in "WHAT IS ALREADY BUILT." Module 1, Module 3, orchestrator, datablender, Salesforce integration, and MSSQL are **greenfield**.

---

## BUSINESS CONTEXT

We sell / pursue wireless infrastructure sites. The pipeline:

| Stage | Name | Purpose |
|-------|------|---------|
| **1** | Site discovery | Find *new potential* cell sites within zip codes or cities |
| **2** | Imagery classification | Use aerial imagery + AI to classify **tower vs rooftop**, detect **cellular equipment**, locate asset, output **confidence metrics** |
| **3** | Ownership lookup | Resolve land parcel or rooftop building owner (open-source or paid API — TBD) |

**Deduplication (all stages):** Check candidates against our **Salesforce** CRM (real-time API and/or external **MSSQL** staging DB — approach TBD). Avoid re-processing or re-creating known accounts/sites.

**Naming preference:** Shared layer = `datablender-core`. Stage packages = `site-discovery`, `site-classifier`, `site-ownership`. Orchestrator = `datablender-orchestrator` or `datablender-flow`.

**Architecture preference:** NOT a single monolith, NOT three disconnected backends. Use **pipeline + shared datablender core** (hub-and-spoke). Phase 1 = monorepo; Phase 2 = split workers sharing `datablender-core` package.

---

## WHAT IS ALREADY BUILT (MODULE 2 ONLY — THIS IS THE ENTIRE STARTING CODEBASE)

Everything below exists today in a single folder. **Nothing else has been built.**

### Repository contents (today)

```
asset-classifier/          # will rename to site-classifier / datablender-classify
├── asset_classifier.py    # ~1000 lines — THE entire pipeline
├── assets.csv             # input: id, lat, lon, label (optional)
├── results.csv            # output: classification results
├── chips/                 # saved imagery for human audit
├── requirements.txt
├── README.md
├── WORKFLOW_GUIDE.md      # plain-English ops doc
├── EXECUTIVE_SUMMARY.md   # auto-generated after runs
├── .env                   # GEMINI_API_KEY, NEARMAP_API_KEY
└── .gitignore
```

No Docker, no tests, no CI, no database, no API server, no Module 1, no Module 3, no orchestrator.

### Module 2 behavior (implemented in `asset_classifier.py`)

**Input:** `assets.csv` with columns `id`, `lat`, `lon`, optional `label` (e.g. `JP`, `W/C`, `stealth`).

**Per-asset sequence:**

1. **Resume check** — skip if row already in `results.csv` without error.
2. **NAIP fetch** (always) — Microsoft Planetary Computer STAC; 250 m × 250 m chip; ~1 m resolution; saves `chips/{id}_NAIP.jpg`.
3. **Nearmap fetch** (if `NEARMAP_API_KEY` set) — Tile API (NOT Transactional API; transactional returns 403 on our subscription). Pulls:
   - `Vert` — high-res top-down (~7 cm)
   - `North`, `East`, `South`, `West` — 45° oblique panoramas
   - 100 m AOI initially; saves `chips/{id}_nearmap_{view}.jpg`
4. **If no NAIP and no Nearmap** → `site_type = no_imagery`, stop.
5. **Primary Gemini classification** — multi-image vision; JSON schema enforced.
6. **Wide Nearmap fallback** — if result is `other`/`unclear` AND no oblique views: re-fetch Nearmap at 250 m AOI, re-classify (`classification_stage = wide_aoi`).
7. **Two-stage zoom fallback** — if still `other`/`unclear` OR `label == stealth`: scout candidate regions on best top-down image, magnify crops, re-classify (`classification_stage = zoom`). Saves `chips/{id}_zoom_N.jpg`.
8. **Geolocation** — if AI box drawn on NAIP, convert to `asset_lat`, `asset_lon`, `asset_offset_m`.
9. **Write `results.csv`** after each asset (crash-safe). ~12 s delay between assets (Gemini rate limit).

**AI outputs (JSON → CSV columns):**

- `site_type`: `tower` | `rooftop` | `other` | `unclear` | `no_imagery`
- `site_confidence`, `site_evidence`
- `cell_equipment`, `cell_equipment_confidence`, `cell_equipment_evidence`
- `asset_lat`, `asset_lon`, `asset_offset_m`, `asset_box_2d`, `asset_view`
- `nearmap_views`, `nearmap_date`, `nearmap_aoi_m`, `image_date`
- `classification_stage`: `primary` | `wide_aoi` | `zoom`
- `zoom_crops`, `model`, `error`

**External dependencies:**

- `GEMINI_API_KEY` — Google Gemini vision (model fallback chain on quota errors)
- `NEARMAP_API_KEY` — optional; bills against ~2.49 GB/month Oblique subscription via Tile API
- NAIP — free, US continental coverage only

**Known imagery branches (must be first-class in orchestrator):**

| `nearmap_views` | Meaning | Ops implication |
|-----------------|---------|-----------------|
| `Vert,North,East,South,West` | Full Nearmap | Highest AI confidence |
| `Vert` only | Rural partial | No obliques; may need zoom / manual review |
| *(empty)* | NAIP only — no Nearmap coverage at coordinate | Set `manual_review_required`; lower trust |
| `no_imagery` | Neither source | Dead end — flag for alternate imagery |

**Pilot learnings:**

- Nearmap obliques critical for rooftop antennas and disguised towers (monopines).
- Coordinates can be 10–60 m off true asset — pipeline searches whole image, not center only.
- Rural sites (e.g. asset_005, asset_007) often lack Nearmap; NAIP + zoom is fallback.
- asset_007 (44.775, -117.83): zero Nearmap surveys; stealth tower visible to human on NAIP only.

---

## INPUT PATHWAYS THE ORCHESTRATOR MUST SUPPORT

### Path A — CSV direct to Module 2 (current behavior)

Operator provides `assets.csv` → run classifier only → `results.csv` + `chips/` + `EXECUTIVE_SUMMARY.md`.

Orchestrator command example (you design):

```bash
datablender run classify --input assets.csv
```

### Path B — Full pipeline Module 1 → 2 → 3

Operator provides discovery scope (zip codes and/or cities) → Module 1 emits candidate `SiteRecord` list → dedup → Module 2 → dedup again → Module 3 → final enriched export.

```bash
datablender run pipeline --zips 07001,07002 --cities "Newark,NJ"
# or
datablender run discover --zips ... > candidates.json
datablender run classify --input candidates.json
datablender run ownership --input results.json
```

**Requirement:** Path B Module 1 output must be convertible to the same shape as `assets.csv` (or a superset `SiteRecord`) so Module 2 needs minimal changes.

---

## MODULE 1 — NOT BUILT (DESIGN STUB)

**Purpose:** Discover potential new cell sites in geographic areas.

**Inputs:** Zip code(s), city name(s), optional bounding box.

**Outputs:** List of `SiteRecord` with at minimum: `site_id`, `lat`, `lon`, `discovery_source`, `discovery_reason`.

**Dedup:** Before emitting, check Salesforce / MSSQL for existing sites within configurable radius (e.g. 50 m).

**Implementation notes for stub:** No specific discovery API chosen yet. Stub should define `DiscoveryProvider` interface (pluggable: FCC ASR, OpenCelliD, commercial feeds, manual seed, etc.).

---

## MODULE 3 — NOT BUILT (DESIGN STUB)

**Purpose:** Ownership resolution for classified sites.

**Inputs:** `SiteRecord` where `site_type` in (`tower`, `rooftop`) and confidence above threshold.

**Outputs:** `owner_name`, `owner_type`, `parcel_id`, `ownership_source`, `ownership_confidence`.

**Dedup:** Match parcel/owner to existing Salesforce accounts.

**Implementation notes for stub:** `OwnershipProvider` interface (Regrid, county assessor, Melissa, etc. — TBD).

---

## DATABLENDER-CORE — NOT BUILT (DESIGN STUB)

Shared library used by orchestrator and all modules:

- `SiteRecord` pydantic model (canonical schema)
- `normalize_coordinates()`
- `derive_imagery_tier(nearmap_views) -> full | nearmap_vert_only | naip_only | none`
- `derive_manual_review_required(site_record) -> bool`
- `DedupService` interface:
  - `check_salesforce(lat, lon, radius_m) -> DedupResult`
  - `check_mssql(lat, lon, radius_m) -> DedupResult` (stub)
- `merge_site_record()` — stage outputs into accumulating record

---

## ORCHESTRATOR REQUIREMENTS

- **Stage isolation:** Each module runnable standalone.
- **Resume:** Per-stage checkpoint files (like today's `results.csv` resume).
- **Retry policy:** Document per failure type (Gemini 503, Nearmap 404, no_imagery, low confidence).
- **Manual review queue:** Export CSV/JSON of records where `manual_review_required=true`.
- **Idempotency:** Re-running same `site_id` should not double-charge Nearmap/Gemini if already succeeded (configurable force-reprocess flag).
- **Logging:** Structured logs per `site_id` and `stage`.
- **Config:** `.env` for API keys; YAML or TOML for radii, confidence thresholds, AOI sizes.

---

## SUGGESTED MONOREPO LAYOUT (YOU MAY REFINE)

```
datablender-site-pipeline/
├── packages/
│   ├── datablender-core/
│   │   ├── models.py          # SiteRecord, DedupResult
│   │   ├── dedup.py           # Salesforce + MSSQL stubs
│   │   └── imagery.py         # imagery_tier, manual_review flags
│   ├── site-discovery/        # Module 1 stub
│   ├── site-classifier/       # MOVE existing asset_classifier.py here
│   └── site-ownership/        # Module 3 stub
├── orchestrator/
│   ├── cli.py                 # datablender run {discover|classify|ownership|pipeline}
│   └── workflow.py            # stage sequencing, handoffs
├── docs/
│   ├── WORKFLOW_GUIDE.md
│   └── ARCHITECTURE.md
├── pyproject.toml             # workspace / uv / poetry
└── examples/
    ├── assets.csv
    └── pipeline_config.yaml
```

---

## TECHNICAL CONSTRAINTS

- **Language:** Python 3.11+
- **Preserve Module 2 logic** — refactor into importable functions/classes; don't discard NAIP/Nearmap/Gemini/zoom fallback behavior.
- **File handoff first** — CSV/JSON between stages; design for future Postgres + job queue without rewriting modules.
- **Windows dev environment** — paths may use backslashes; use `pathlib`.
- **Secrets:** `python-dotenv`, never commit `.env`.
- **No over-engineering** — stubs are fine; real discovery/ownership APIs are TBD.

---

## QUESTIONS YOU SHOULD ANSWER IN YOUR RESPONSE

1. Exact `SiteRecord` field list with which stage writes each field.
2. CLI commands and example invocations for Path A and Path B.
3. State machine diagram for a single `site_id` through all stages.
4. How `manual_review_required` and `imagery_tier` are set.
5. How Salesforce dedup hooks in at each stage (even as stubs).
6. Migration plan: current `asset-classifier/` folder → new monorepo structure in minimal steps.
7. What to build first (suggested sprint order).

Generate production-quality scaffolding code where possible, with clear `TODO` markers for TBD integrations.
