# Enrichment metrics (Azure SQL)

Leadership KPIs for site-type enrichment live in **Symphony_dev**. They are written at the **end of each `python -m enrichment` run**, not as a separate ingest.

## Run completion (order)

1. Salesforce queue pull (blank `Site_Type__c`).
2. Per site: FCC / TowerSource proximity **reads** (tower ingest).
3. Imagery classify (NAIP → OSM → Nearmap → Gemini/Claude) when the DB skip does not fire.
4. Write run CSVs under `../site-orchestrator-data/runs/<run_id>/`.
5. Optional Salesforce **apply**.
6. **`record_run`**: append local JSONL, rewrite `kpis.json`, upsert SQL (fail-open).

If SQL is down, CSVs and Salesforce writes still stand. Set `METRICS_SQL=0` to skip SQL and keep JSONL only.

Local fallback (same grain as SQL):

- `../site-orchestrator-data/metrics/runs.jsonl`
- `../site-orchestrator-data/metrics/sites.jsonl`
- `../site-orchestrator-data/metrics/kpis.json` (last Salesforce Id wins)

## Objects (2 tables, 4 views)

| Object | Kind | Grain |
|---|---|---|
| `dbo.EnrichmentRun` | table | one row per `run_id` |
| `dbo.EnrichmentSiteOutcome` | table | `(RunId, SalesforceId)` |
| `dbo.vEnrichmentSiteLatest` | view | last observation per Salesforce Id |
| `dbo.vEnrichmentKpis` | view | last-Id-wins totals |
| `dbo.vEnrichmentKpisByState` | view | last-Id-wins by `SiteState` |
| `dbo.vEnrichmentKpisByMatchSource` | view | last-Id-wins by FCC / TowerSource / none |

Do not store last-wins KPIs on `EnrichmentRun`. That header is **this run only**. Retries must not double-count; use the views.

## What belongs where

**`EnrichmentRun` (ops header)**

- This-run counts: sites, applied rooftop/tower/DB-skip, holdouts, errors
- Spend proxies: `NearmapSites`, `ClaudeSites`
- Funnel: NAIP-empty → Nearmap → rooftop apply + rate
- Salesforce: `SfWrites`, `SfHoldoutsDequeued`, `SfWriteFailed`
- Queue: `ApplyEnabled`, `QueueStates`, `QueueLimit`

**`EnrichmentSiteOutcome` (fact)**

- Identity: `SalesforceId`, `Address`, `SiteState`, `SiteCity`, `Carrier`
- Path: `MatchSource` (`FCC` / `TowerSource` / `none`), `ClassifyCoordSource`, `AssetOffsetM`
- Vision: `ScreenSiteType`, `FinalSiteType`, `FinalConfidence`
- Spend: `NearmapRan`, `NearmapTier`, `ClaudeRan`, `EscalationReason`, `SecondNearmap`, `DualModelResolution`
- Funnel bits: `EmptyToNearmap`, `EmptyToRooftop`, `EmptyToRooftopApply`
- Decision: `Bucket`, `HoldoutReason`, `UpdateSiteType`, `Outcome`, `SfUpdateStatus`

Do **not** store chips, prompts, raw model JSON, or dollar estimates (packs/tokens are not metered yet).

## Outcomes

| `Outcome` | Meaning |
|---|---|
| `applied_rooftop` | Salesforce write, rooftop |
| `applied_tower` | Salesforce write, tower (imagery path) |
| `applied_db_skip` | Unique FCC/TowerSource ≤ 25 m, skipped imagery |
| `holdout_empty_confirmed` | Nearmap other/unclear locked at ≥ 0.90 |
| `holdout_weak_rooftop` | Rooftop label below apply bar |
| `holdout_weak_tower` | Tower label below apply bar |
| `holdout_empty` | other/unclear, not locked |
| `holdout_no_nearmap` | Nearmap `no_coverage` |
| `error` | classify/SQL/missing coords |

## Queries

```sql
SELECT * FROM dbo.vEnrichmentKpis;
SELECT * FROM dbo.vEnrichmentKpisByState;
SELECT * FROM dbo.vEnrichmentKpisByMatchSource;
SELECT * FROM dbo.vEnrichmentSiteLatest;
SELECT * FROM dbo.EnrichmentRun ORDER BY RecordedAt DESC;
```

## Backfill / schema

DDL is `sql/enrichment_metrics.sql` (SSMS or the Python loader). The pipeline runs the same file on each SQL upsert so new columns/views appear.

```powershell
python scripts/load_enrichment_metrics.py          # create/alter + load JSONL
python scripts/load_enrichment_metrics.py --dry-run
```

Idempotent for run ids already in `metrics/runs.jsonl`. Historical rows are hydrated from `runs/<run_id>/enrichment_detail.csv` when present.

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
