# Enrichment metrics (Azure SQL)

Leadership KPIs for site-type enrichment live in **Symphony_dev**. They are written **during each live `python -m enrichment` run** (`APPLY=1`), not as a separate ingest.

## When rows are written

1. Run start: the `dbo.EnrichmentRun` header is upserted (so site rows can reference it).
2. Each site, as soon as it finishes classify + Salesforce apply: one `dbo.EnrichmentSiteOutcome` row is upserted. A crash keeps every completed site.
3. Run end: **`record_run`** appends the local JSONL ledger, rewrites `kpis.json`, and reconciles the whole run in SQL (header + all site rows).

All SQL writes are fail-open: if Azure SQL drops, the run and its Salesforce writes continue, and the end-of-run reconcile retries. Set `METRICS_SQL=0` to skip SQL and keep JSONL only. `APPLY=0` dry-runs write a local run header (`apply_enabled=0`) and nothing else.

Local fallback (same grain as SQL):

- `../site-orchestrator-data/metrics/runs.jsonl` — one header per run
- `../site-orchestrator-data/metrics/sites.jsonl` — one row per processed site per live run
- `../site-orchestrator-data/metrics/kpis.json` — cumulative rollup

## Counting rules

Headline KPIs for leadership (`dbo.vEnrichmentKpis`, `kpis.json`):

| Question | KPI (column) | Definition |
|---|---|---|
| How much ground did we cover? | **Sites processed** (`UniqueSites`) | Distinct Salesforce Ids a live run evaluated — applied, held out, DB-only miss, failed apply, skipped, or error |
| How much did we deliver? | **Sites enriched** (`WrittenSites`) | Distinct Ids with a Salesforce-accepted Site_Type write (`sf_update_status=updated`) |
| How well does it convert? | **Enrichment yield** (`TotalWriteRate`) | Sites enriched ÷ sites processed |
| What did we deliver? | **Tower share / Rooftop share** (`TowerWriteRate` / `RooftopWriteRate`) | `TowerSfWrites` / `RooftopSfWrites` ÷ sites enriched — by the Site_Type written, whether imagery or the tower database decided it |
| How cheaply? | **DB-match share** (`DbMatchRate`) | `AppliedDbSkip` (unique FCC/TowerSource hit, no imagery or AI spend) ÷ sites enriched |

Supporting counts:

| KPI | Definition |
|---|---|
| Outcome counts (holdouts, errors, misses) | Each Id's **latest** observation. A site held out in one run and written in a later run counts as written, once. |
| NearmapSites / ClaudeSites | Ids where paid imagery / Claude **ever** ran (spend proxy, not applies). |
| NearmapMB / NearmapMBPerEnriched | Billed Nearmap megabytes (cache hits are free) across all live runs, and per site enriched. Metered from 2026-09-24; earlier runs count 0. Site rows carry `NearmapBytes` / `NearmapTiles` / `NearmapCacheHits`; `enrichment_detail.csv` also has `nearmap_spend` by stage. |
| NaipEmptyToNearmap / …RooftopApply | Ids whose NAIP screen was empty but Nearmap ran / then wrote a rooftop. |

`TowerSfWrites + RooftopSfWrites = WrittenSites` (plus the rare `applied_other`). Imagery-decided writes = `WrittenSites − AppliedDbSkip`. Yield swings with the queue: DB-only runs over wide stages evaluate many sites with no tower match, which lowers yield without anything being wrong — slice by `MatchSource` or run type to compare like with like.

## Objects (2 tables, 7 views)

| Object | Kind | Grain |
|---|---|---|
| `dbo.EnrichmentRun` | table | one row per `run_id` (this-run ops header) |
| `dbo.EnrichmentSiteOutcome` | table | `(RunId, SalesforceId)` — every processed site |
| `dbo.vEnrichmentSiteLatest` | view | latest observation per Salesforce Id |
| `dbo.vEnrichmentSiteLatestWrite` | view | latest successful write per Salesforce Id |
| `dbo.vEnrichmentSiteFacts` | view | one row per Id: latest outcome + write flags + "ever ran" spend |
| `dbo.vEnrichmentKpisByDimension` | view | KPI columns once, `GROUPING SETS` over all / SiteState / MatchSource |
| `dbo.vEnrichmentKpis` | view | `Dimension = 'all'` |
| `dbo.vEnrichmentKpisByState` | view | `Dimension = 'state'` |
| `dbo.vEnrichmentKpisByMatchSource` | view | `Dimension = 'match_source'` |

Do not store last-wins KPIs on `EnrichmentRun`. That header is **this run only**.

## What belongs where

**`EnrichmentRun` (ops header)**

- This-run counts per outcome (see below), `Errors`
- Spend proxies: `NearmapSites`, `ClaudeSites`
- Funnel: NAIP-empty → Nearmap → rooftop apply + rate
- Salesforce: `SfWrites`, `SfHoldoutsDequeued`, `SfWriteFailed`
- Queue: `ApplyEnabled`, `QueueStates`, `QueueLimit`

**`EnrichmentSiteOutcome` (fact)**

- Identity: `SalesforceId`, `Address`, `SiteState`, `SiteCity`, `Carrier`
- Path: `MatchSource` (`fcc` / `towersource` / `none`), `ClassifyCoordSource`, `AssetOffsetM`
- Vision: `ScreenSiteType`, `FinalSiteType`, `FinalConfidence`
- Spend: `NearmapRan`, `NearmapTier`, `ClaudeRan`, `EscalationReason`, `SecondNearmap`, `DualModelResolution`
- Funnel bits: `EmptyToNearmap`, `EmptyToRooftop`, `EmptyToRooftopApply`
- Decision: `Bucket`, `HoldoutReason`, `UpdateSiteType`, `Outcome`, `SfUpdateStatus`

Column lists live once in `enrichment/metrics_store.py` (`RUN_COLUMNS`, `SITE_COLUMNS`); the SQL statements are generated from them. Do **not** store chips, prompts, raw model JSON, or dollar estimates.

## Outcomes

| `Outcome` | Meaning |
|---|---|
| `applied_rooftop` | Salesforce write, rooftop (imagery) |
| `applied_tower` | Salesforce write, tower (imagery) |
| `applied_db_skip` | Unique FCC/TowerSource ≤ 25 m, skipped imagery |
| `applied_other` | Salesforce write, other picklist (rooftop confirm keeping a sales type) |
| `apply_failed` | Eligible write Salesforce refused (dequeued by the holdout retry, or failed) |
| `holdout_empty_confirmed` | Nearmap other/unclear locked at ≥ 0.90 |
| `holdout_weak_rooftop` | Rooftop label below apply bar |
| `holdout_weak_tower` | Tower label below apply bar |
| `holdout_empty` | other/unclear, not locked |
| `holdout_no_nearmap` | Nearmap `no_coverage` |
| `holdout_no_imagery` | No NAIP or Nearmap imagery at the pin |
| `holdout_other` | Anything else held out |
| `holdout_nearmap_budget` | Needed Nearmap after the monthly budget ran out; stays in the Salesforce queue |
| `db_only_miss` | `DB_ONLY=1`, no unique FCC/TowerSource hit |
| `skipped` | No saved chips to rerun / skip-classify without a DB hit |
| `error` | classify / SQL / missing coords |

`applied_*` is the decision; `SfUpdateStatus=updated` is what makes it a write.

## Queries

```sql
SELECT * FROM dbo.vEnrichmentKpis;
SELECT * FROM dbo.vEnrichmentKpisByState;
SELECT * FROM dbo.vEnrichmentKpisByMatchSource;
SELECT * FROM dbo.vEnrichmentSiteFacts;
SELECT * FROM dbo.vEnrichmentSiteLatestWrite;
SELECT * FROM dbo.EnrichmentRun ORDER BY RecordedAt DESC;
```

## Backfill / schema

DDL is `sql/enrichment_metrics.sql` (SSMS or the Python loader). The pipeline runs the same file when it opens its metrics connection, so new columns/views appear automatically.

```powershell
python scripts/load_enrichment_metrics.py --dry-run                        # counts only
python scripts/load_enrichment_metrics.py                                  # upsert the JSONL ledger
python scripts/load_enrichment_metrics.py --backfill-processed --dry-run   # preview history rebuild
python scripts/load_enrichment_metrics.py --backfill-processed             # rebuild history
```

`--backfill-processed` rebuilds `sites.jsonl` from each live run's `enrichment_detail.csv` (statuses filled from `sf_update_apply_log.csv` for old runs), so runs recorded when the ledger only kept successful writes also count their holdouts, misses, and errors. It then replaces every `EnrichmentSiteOutcome` row. Runs whose detail CSV is gone keep their existing ledger rows.

## AR_TMO_jan2025 cohort (seed)

10 unique sites, last Id wins (runs `2026-08-28_132628` then holdout retry `150606`):

| Slice | Unique sites | Rooftop SF writes |
|---|---|---|
| All | 10 | 5 (50%) |
| DC | 3 | 3 |
| CO | 1 | 1 |
| KS | 6 | 1 |
| MatchSource `none` | 10 | 5 |

Empty-to-rooftop apply rate on the retry funnel: 2 of 6 (33%). Remaining holdouts were manually empty (no transactable macro gear).
