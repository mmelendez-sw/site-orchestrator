# ADR 0001: Multi-source evidence cascade for rooftop verification (ICEMAN)

- **Status:** Proposed. Implemented behind flags on `dev-multi-source-imagery`; nothing is enabled by default.
- **Date:** 2026-10-07
- **Owner:** Matt Melendez
- **Scope:** `python -m enrichment` (including `CONNECTX_AUDIT`), `classifier/`, operator tooling.

## Context

ICEMAN decides whether a Salesforce `Site__c` has cellular equipment and what kind of structure hosts it. Today's evidence chain is:

1. FCC ASR and TowerSource proximity (SQL)
2. An OSM prefilter
3. NAIP at 60 cm
4. Paid Nearmap aerial imagery: a top-down view and angled ("oblique") views
5. A Gemini/Claude dual-model confirm

The October 2026 ConnectX reconciliation exposed the limits of that chain.

| Observation | Number |
|---|---|
| `LLM_Classified` rooftops in Salesforce | 4,902 |
| …whose only "verification" is `Verbal Confirmation` | 3,184 (65%) |
| …verified by NearMap | 757 |
| Known-real sites where NAIP saw gear | 5 of 162 |
| Pool audit, 632 sites: confirmed rooftop / other type / no asset / inconclusive | 218 / 139 / 41 / 234 |
| Nearmap spend for those 632 sites | ≈ 290 MB, against a monthly cap raised twice to 2,750 MB |
| Gemini 503 ("high demand") warnings in one 35-minute, 10-lane run | 315, causing 50 site failures |

The table points to four problems:

- **NAIP can't see rooftop gear**, so every undecided rooftop goes to Nearmap.
- **Nearmap is the bottleneck.** It is the scarce, metered resource, and its budget was enforced per process, so parallel lanes could overshoot it.
- **More than a third of audited sites end inconclusive**, mostly "gear seen, not confirmed." That spend buys no decision.
- **Throughput depends on one preview Gemini model** with no fallback.

## Decision drivers

1. Nearmap MB per *decided* site, not per processed site.
2. Precision of writes. A false "confirmed" sends a rep to a dead site; a false "no asset" removes a real one.
3. Data licensing.
4. Operability: parallel runs, budget safety, progress visibility.
5. No behaviour change unless explicitly enabled.

## Options considered

| Option | Verdict |
|---|---|
| Buy more Nearmap | Linear cost. Leaves the inconclusive rate and the budget-race problem unsolved. |
| Higher-resolution satellite (Maxar, Planet) | Paid, contract-heavy, and top-down only. Doesn't fix the side-view gap. |
| Sentinel-2 / Landsat | 10–30 m resolution. Useless for antennas. |
| **Evidence cascade: cheap signals and free or cheap imagery first, Nearmap only for the undecided** | **Chosen.** |

## Decision

Run evidence in tiers ordered by cost. Each tier can only *add* evidence. The existing write gates remain the single place where Salesforce decisions are made.

```
 Tier 0  Signals (free)      FCC ULS microwave · OpenCelliD · OSM antenna tags     → signal columns, prioritization
 Tier 1  Registries (free)   FCC ASR · TowerSource (existing)                      → may settle towers (existing rule)
 Tier 2  Top-down (free)     State/county orthoimagery (3–15 cm) where configured  → added view; outranks NAIP
                             NAIP (existing)
 Tier 3  Street-level        Mapillary (free)                                      → added views facing the site
 Tier 4  Paid aerial         Nearmap top-down + angled views (existing, budgeted)  → required for rooftop writes by default
 Tier 5  Models              Gemini primary → GA fallback on 503; Claude dual-model (existing)
```

### Components

| Component | Module | Notes |
|---|---|---|
| Supplemental imagery | `classifier/sources/` (`state_ortho`, `mapillary`, `registry`) | `fetch_supplemental_views()` never raises. Results are cached on disk and metered per source (`supplemental_*` columns). |
| Signals | `enrichment/signals/` (`uls`, `opencellid`, `osm_antenna`) | `collect_signals[_bulk]()` produces `uls_*`, `opencellid_*`, `osm_antenna_count` and `signal_strength`. |
| FCC ULS data | `sql/fcc_uls_microwave.sql`, `scripts/load_fcc_uls_microwave.py` | Weekly bulk load of active microwave license locations into `dbo.FccUlsMicrowaveLocation`. |
| View policy | `classifier/views.py` | Model view order: Nearmap top-down, then state orthoimagery, then NAIP, followed by angled and street-level views. |
| Shared budget | `classifier/imagery.py`, `enrichment/budget.py` | Each Nearmap purchase is appended to `metrics/nearmap_purchases.jsonl`. Every process re-reads it, so lanes share one budget. |
| Model resilience | `classifier/asset_classifier.py` | `GEMINI_FALLBACK_MODEL` plus a circuit breaker. `classify_site` now honours `GEMINI_RETRIES`. |
| Operations | `enrichment/lanes.py` | Parallel lanes, shared rate split, budget and coverage stops, status table and JSON, graceful flush. |
| Measurement | `scripts/build_eval_set.py`, `scripts/eval_report.py` | Labels come from Salesforce outcomes. Precision and recall are reported per tier and per source. |

### Write policy (unchanged unless flagged)

- **Rooftop "confirmed"** still requires Nearmap angled views plus dual-model agreement and a located asset (`enrichment/bucketing.py`).
- **Rooftop "no asset"** (audit unqualify) still requires Nearmap angled views with no gear seen by any model, no hedging, no stealth host, and no registry record within 100 m (`enrichment/connectx_audit.py`).
- **`SUPPLEMENTAL_CAN_CONFIRM=1`** (default off) lets a street-level view stand in for an angled view in the rooftop confirm gate, still requiring dual-model agreement and an asset box on that view. `Verified_Site_Source__c` is then **Google Map** for Mapillary. State orthoimagery has no picklist value yet, so those confirmations are held out until a Salesforce admin adds `State Orthoimagery`.
- **Signals never write by themselves.** They are evidence columns and inputs to prioritization. A strong ULS hit raises the site's priority for Nearmap; it does not confirm the site.

### Configuration (all off by default)

| Variable | Purpose |
|---|---|
| `SUPPLEMENTAL_IMAGERY=state_ortho,mapillary` | Enable sources, in priority order |
| `STATE_ORTHO_SOURCES=path.json` | Registry of state and county imagery services (see `docs/state_ortho_sources.example.json`) |
| `MAPILLARY_ACCESS_TOKEN` | Mapillary Graph API |
| `SIGNALS=1`, `SIGNALS_SOURCES=uls,opencellid,osm`, `OPENCELLID_API_KEY` | Tier 0 signals |
| `SUPPLEMENTAL_CAN_CONFIRM=1` | Street-level evidence may satisfy the rooftop and tower confirm gates (Claude localize, conf >= 0.9, no hedges, photo <= `STREET_CONFIRM_MAX_AGE_YEARS`) |
| `SAVED_NEARMAP_CHIPS=1` | Reuse Nearmap chips earlier runs bought |
| `AUDIT_HOLDOUT_OWNER=<User Id>` | Audit: holdout + reassign unconfirmed sites instead of unqualifying |
| `GEMINI_FALLBACK_MODEL`, `GEMINI_FALLBACK_AFTER`, `GEMINI_FALLBACK_COOLDOWN_S` | Model resilience |
| `NEARMAP_BUDGET_REFRESH_S` | How often each process re-reads the shared ledger |

## Rollout

1. **Measure.** Build the evaluation set (`scripts/build_eval_set.py`) and produce a baseline report on existing runs.
2. **Signals + ULS load.** Load `dbo.FccUlsMicrowaveLocation`, then run with `SIGNALS=1` on a dry run (`APPLY=0`) of the ~1,400 remaining pool rooftops. Check whether `signal_strength=strong` predicts confirmation well enough to reorder the queue.
3. **Supplemental imagery, evidence only.** Turn on `SUPPLEMENTAL_IMAGERY=state_ortho,mapillary` for DC and NY first, where both coverage and today's demand are concentrated. Compare the inconclusive rate and Nearmap MB per decided site against the baseline.
4. **Street-level confirms.** Trial `SUPPLEMENTAL_CAN_CONFIRM=1` with Mapillary on a labelled sample. Promote it only if precision on the evaluation set is at least as good as the Nearmap-only gate.
5. **Model fallback.** Set `GEMINI_FALLBACK_MODEL` to a GA model once its accuracy on the evaluation set matches the preview model.

**Success metrics:**
- Inconclusive rate below 20% (now 37%).
- Nearmap MB per decided rooftop down 30% or more.
- Precision of written confirmations at least the current level on the evaluation set.
- Zero budget overshoot across parallel lanes.

## Consequences and risks

- **Licensing:**
  - Google Street View was evaluated and dropped: Google Maps Platform terms restrict caching Street View imagery and deriving data from it, which is what this pipeline does.
  - Mapillary imagery is CC BY-SA; derived records need attribution in reports.
  - OpenCelliD data is CC BY-SA 4.0.
  - State orthoimagery terms vary by agency and are recorded per entry in the registry.
- **Coverage varies.** Mapillary is sparse outside metros, state orthoimagery exists only where an agency publishes it, and both can be older than Nearmap. Labels carry capture dates so the models can weigh recency.
- **OpenCelliD positions are coarse estimates**, so it is only ever a weak signal.
- **More views mean more model tokens per call.** This is bounded by per-source view caps.
- **New operational job:** a weekly FCC ULS load.

## Follow-ups (Salesforce admin)

1. Add `State Orthoimagery` (and optionally `FCC ULS`) to `Verified_Site_Source__c`.
2. Separate "processed" from "verified". Keep `LLM_Classified__c` as the processing marker and add a verification status picklist (`Unverified / Imagery Verified / Registry Verified / Rep Verified / No Asset`), so imported Verbal Confirmation rooftops no longer look checked.
