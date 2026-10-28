# ICEMAN

ICEMAN decides whether a Salesforce `Site__c` record has cellular equipment and what structure hosts it (tower subtype, rooftop, stealth, water tower…), then writes the result back to Salesforce. It works through an evidence cascade, from cheapest to most expensive, and stops as soon as the evidence is strong enough:

1. **FCC / TowerSource** registry match (Azure SQL). A unique hit within 25 m writes `Site_Type`, coordinates and `Verified_Site_Source` with no imagery at all.
2. **Free evidence:** Overture building footprints (pin check), NAIP aerials, Mapillary street photos, FCC ULS microwave and OpenCelliD signals, and Nearmap imagery an earlier run already bought.
3. **Gemini** classifies the views; **Claude** independently confirms cellular gear (dual-model agreement).
4. **Nearmap** Vert + obliques (paid, metered against a monthly budget) only for sites that still need high-resolution sides.

Every decision passes the write gates in [enrichment/bucketing.py](enrichment/bucketing.py) before anything reaches Salesforce. Sites that cannot be decided are held out (`LLM_Holdout__c = true`) for human review rather than guessed.

Design background: [docs/adr/0001-multi-source-evidence-cascade.md](docs/adr/0001-multi-source-evidence-cascade.md). Every setting: [docs/configuration.md](docs/configuration.md). KPIs: [docs/enrichment-metrics.md](docs/enrichment-metrics.md). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env      # fill in Salesforce, Gemini, Anthropic, Nearmap, Azure SQL, Mapillary
az login                    # Azure SQL uses Entra token auth
```

Also install the **ODBC Driver 18 for SQL Server**. Run folders, chips, caches and metrics live in `../site-orchestrator-data` (override with `SITE_ORCHESTRATOR_DATA`), never in git.

Check the install offline (no paid APIs, Salesforce or SQL):

```powershell
python -m pytest -q
python -m ruff check .
```

## Operating ICEMAN

### Ground rules

- **Dry run first.** `APPLY=0` (`--env APPLY=0` on lanes) classifies and writes CSVs without touching Salesforce. Review the detail CSV, then run with `APPLY=1`. Lanes `--dry-run` only prints the batch plan.
- **Check the Nearmap budget before any run that can buy imagery:** `python -m enrichment.budget`. Use `--profile free` when no new Nearmap should be bought.
- **One launcher.** Use `python -m enrichment.lanes` for anything bigger than a few dozen sites. It shares the model rate limits and the Nearmap budget across batches; separate terminals do not share the rate limits.
- Salesforce writes are batched and logged per run (`sf_update_apply_log.csv`). Every finished site is in `enrichment_detail.csv` before its batch is sent, so nothing finished is lost on a crash.

### Standard run (lanes)

```powershell
# Zero new Nearmap spend; holdouts go to the given owner. Plan only:
python -m enrichment.lanes --ids-file ids.txt --profile free --lanes 10 --batch 25 `
  --env CONNECTX_AUDIT=1 --env AUDIT_HOLDOUT_OWNER=<User Id> --run-name holdouts --dry-run

# Live (drop --dry-run); cap Nearmap at the month-to-date MB you are willing to reach:
python -m enrichment.lanes --pool-audit --lanes 5 --batch 75 --stop-at-mb 2400 --stop-when-covered
```

| Option | Meaning |
|---|---|
| `--ids-file` / `--pool-audit` | Queue: one Salesforce Id per line, or the Site Acquisition Team's ConnectX rooftops in New/Unreviewed minus Ids an earlier live audit already decided |
| `--priority-csv` | `Id,tier` CSV from `scripts/build_nearmap_priority.py`; lower tiers run first so the budget buys the likeliest confirms |
| `--profile free` | `NAIP_ONLY=1` plus every free source: no new Nearmap (saved and tile-cached Nearmap are still used) |
| `--stop-at-mb` | Stop launching batches at this month-to-date Nearmap MB; running batches finish |
| `--stop-when-covered` | Stop once confirmed pool rooftops cover the open rep slots |
| `--env KEY=VALUE` | Passed to every batch (repeatable) |

Each batch gets its own run folder `runs/<stamp>_<name>_L<lane>_C<chunk>_…`; the lanes folder `runs/<stamp>_lanes_<name>/` holds `plan.json`, `status.json`, `events.log` and one log per batch. Every stop ends with a flush that pushes any finished-but-unsent sites to Salesforce.

### Monitoring

```powershell
python scripts/run_status.py <run-name> --date 2026-10-28   # batches, verdicts, SF writes, Nearmap MB
python -m enrichment.budget                                  # month-to-date Nearmap by day, run and purpose
```

### Recovery

- **Crash or Ctrl+C during classify:** re-push what finished, without re-classifying (add the same mode flags the run used, e.g. `CONNECTX_AUDIT`):
  ```powershell
  $env:APPLY="1"; $env:APPLY_EXISTING="1"; $env:RUN_DIR="<run folder>"; python -m enrichment
  ```
- **Salesforce rejects a write** (duplicate rule, validation): the site falls back to `LLM_Classified=false`, `LLM_Holdout=true` so it leaves the queue; the error is in `sf_update_apply_log.csv`.
- **Gemini 429/503:** all workers pause together. If `WARNING Gemini 429` repeats, lower `GEMINI_RPM`. `GEMINI_FALLBACK_MODEL` retries on a GA model and opens a circuit breaker after `GEMINI_FALLBACK_AFTER` failures.

## Run modes

All modes run through `python -m enrichment` (one process) or `python -m enrichment.lanes` (many). Every mode honours `APPLY`, `LIMIT`, `IDS`, `STATES`, `STAGES`, `OWNERS`, `CARRIER_LIKE`, `METRO_CLASSIFICATION`, `SKIP_FROM` and `RUN_DIR`.

| Mode | Set | Queue | Writes |
|---|---|---|---|
| Blank-type enrichment (default) | — | `Site_Type` blank, `LLM_Classified=false`, `Stage = Outreach - Verified` unless `STAGES` is set | `Site_Type`, coords, `Verified_Site_Source`, `LLM_Classified`; undecided sites held out |
| ConnectX audit | `CONNECTX_AUDIT=1` | Rep-owned ConnectX rooftops (`AUDIT_POOL=1`: the Site Acquisition Team's pool) | See verdict table below |
| Registry only | `DB_ONLY=1` | Blank type across lead stages, any owner | FCC/TowerSource hits only; no imagery or models |
| NAIP rooftop confirm | `CONFIRM_ROOFTOP=1` | Existing `Site_Type=Rooftop` | Confirms a building roof from NAIP + Gemini; no Nearmap or Claude |
| Holdout re-score | `RERUN_HOLDOUTS_FROM=<date>`, `REUSE_CHIPS_FROM=<date>` | Earlier holdouts | Re-classifies on already-bought chips (`error=no_saved_chips` when none) |

ConnectX audit verdicts:

| Verdict | When | Salesforce write |
|---|---|---|
| `confirmed` | Normal write gates passed | Usual `Site_Type` / coords / `Verified_Site_Source`; owner and stage unchanged |
| `no_asset` | Nearmap obliques reviewed, no model saw gear, confident unhedged call, no stealth host, no registry record within `AUDIT_DB_VETO_M` (100 m), no strong ULS/OSM signal | Owner → Site Acquisition Team (or `AUDIT_UNQUALIFY_OWNER`), `Stage = Unqualified`, reason `No Site/Decommissioned` |
| `inconclusive` | Anything else (NAIP only, budget stop, disputed gear, weak call) | None; stays with its rep |

With `AUDIT_HOLDOUT_OWNER=<User Id>` the unqualify step is replaced: every `no_asset` and `inconclusive` site gets only `LLM_Holdout__c=true` and that owner. `AUDIT_RETRY_INCONCLUSIVE=1` retries inconclusive sites (budget stops and errors are always retried) and requires a second passing review before any imagery confirm.

**ConnectX pull and hot swap.** `python -m enrichment.reconcile_pull` moves not-worked ConnectX rooftops from rep books back to the Site Acquisition Team (sites a rep touched since assignment stay) and records each rep's "owed" count in `swaps/pull_ledger.csv`. After the pool audit, `python -m enrichment.reconcile_swaps` gives each rep one verified pool rooftop per owed site (same state first); `swaps/swap_ledger.csv` keeps a site from being used twice. Both only write a plan unless `PULL_APPLY=1` / `SWAP_APPLY=1`.

## What ICEMAN writes

- **Registry hit:** unique FCC/TowerSource match ≤ 25 m (or the pin on the structure ≤ 5 m) writes type and coordinates with no imagery.
- **Towers:** Gemini tower + cell at site confidence ≥ 0.85 and gear confidence ≥ `GEMINI_TOWER_LOCK_CELL_CONF` (0.80) auto-apply; Claude can confirm weaker towers.
- **Rooftops:** Nearmap obliques + dual-model agreement, or NAIP-only when site and cell confidence ≥ 0.95 with named gear, unhedged evidence and an asset box. A Mapillary photo may stand in for an oblique only with Claude's own box, cell confidence ≥ 0.9, no hedged wording and a photo ≤ `STREET_CONFIRM_MAX_AGE_YEARS` (5) old; those write `Verified_Site_Source = Google Map`.
- **Consistency re-check** (`CONFIRM_CONSISTENCY`, on): each imagery-only confirm is re-classified on its saved chips and written only when both passes agree; a disagreement holds out as `consistency_disagree`.
- **Everything else** holds out (`LLM_Classified=false`, `LLM_Holdout=true`) with a `holdout_reason` in the detail CSV.

## Evidence sources (on by default)

Each can be turned off with `0` / `none`. Full list of knobs: [docs/configuration.md](docs/configuration.md).

| Source | Setting | Notes |
|---|---|---|
| Building-footprint pin check | `FOOTPRINT_PIN_CHECK` | Overture outlines in `dbo.OvertureBuilding`. Off-building pins snap to a building within 60 m (imagery centre moves ≤ 30 m); pins with no building within 60 m go to `review_queue.csv` but still get imagery (monopoles have no footprint) |
| Saved Nearmap reuse | `SAVED_NEARMAP_CHIPS`, `NEARMAP_CACHE_ONLY` | Classify on Nearmap any earlier run bought, and stitch views from tiles cached for neighbours; never a new Nearmap call |
| Mapillary street photos | `SUPPLEMENTAL_IMAGERY=mapillary`, `SUPPLEMENTAL_CAN_CONFIRM` | Needs `MAPILLARY_ACCESS_TOKEN`. Up to 3 views facing the site, cropped toward the bearing |
| Cheap signals | `SIGNALS`, `SIGNALS_SOURCES=uls,opencellid[,osm]` | FCC ULS microwave (Azure SQL) and OpenCelliD (`OPENCELLID_API_KEY`, 1,000 requests/day). A strong signal within 30 m blocks an audit unqualify |
| Disputed-site obliques | `DISPUTE_EXTRA_OBLIQUES` | When Gemini sees gear and Claude does not, buy the oblique directions not yet seen |
| OSM prefilter | `OSM_PREFILTER` | Overpass check before Nearmap when NAIP is inconclusive |

`FORCE_ROOF_SCOUT=1` (off) always buys the wide scout for large roofs such as arenas.

## Nearmap spend

Every tile is metered: bytes, tiles, cache hits and the purchase stage (`pack`, `oblique_extra`, `wide`, `second`, `recenter`). Per site it lands in `enrichment_detail.csv` and Azure SQL; every purchase is appended to `metrics/nearmap_purchases.jsonl`, which every process re-reads every `NEARMAP_BUDGET_REFRESH_S` (5 s), so parallel lanes enforce one budget.

| Purchase | Tiles | When |
|---|---|---|
| Vert + first oblique (100 m, z20) | ~40 | Sites that need Nearmap |
| Remaining obliques | ~20 each | Only when Vert + first oblique did not lock the call |
| Wide host scout (250 m, z19 Vert) | ~25 | Rooftop with cell unconfirmed |
| Re-center pack (100 m) | ~60 | Scout found a candidate off the pin |
| Second pack at the Census point | ~60 | Pin and address disagree |

`NEARMAP_MONTHLY_BUDGET_MB` (default 1500) is compared with this month's ledger plus `NEARMAP_PRIOR_USE_MB` (use the ledger cannot see). At `NEARMAP_BUDGET_SOFT_PCT` (90%) optional buys stop; at 100% all Nearmap stops and those sites hold out as `nearmap_budget` without being dequeued, so next month's run picks them up. The ledger counts bytes differently from the Nearmap portal; reconcile against the portal's tile count before changing the budget.

## Throughput

One process classifies `CLASSIFY_WORKERS` sites at once (default 10, max 32). Pins within `PIN_CLUSTER_M` stay in one worker so nearby pins share imagery. Model calls share one limiter per process (`GEMINI_RPM` 120, `CLAUDE_RPM` 50). Lanes split `--gemini-rpm-total` (default `GEMINI_RPM` or 60) and `--claude-rpm-total` (default `CLAUDE_RPM` or 50) across lanes. The ceiling is roughly `GEMINI_RPM` ÷ 2–5 Gemini calls per site, per minute.

Before classifying, the run geocodes the whole queue in Census batches (cached) and looks up FCC/TowerSource for all pins in a few joins. Salesforce writes go out in sObject Collections batches of `APPLY_BATCH_SIZE` (25; lanes use 10) or every `APPLY_FLUSH_S` (60 s). Imagery is cached under `../site-orchestrator-data/cache/` (`IMAGERY_CACHE=0` disables it; check the Nearmap agreement on tile retention).

## Changing decision rules

1. **Replay** recent decisions through the new rules (no imagery, models or Salesforce). It prints every site whose bucket, holdout reason or `Site_Type` would change and exits 1 if any did:
   ```powershell
   python scripts/replay_decisions.py 2026-10
   ```
2. **Score accuracy** against Salesforce outcomes (Working - Connected / Qualified = positive; Unqualified "No Site" / "Not a Cellular tower" = negative):
   ```powershell
   python scripts/build_eval_set.py --carrier-like ConnectX --site-type Rooftop
   python scripts/eval_report.py --eval <data>/eval/eval_set_<date>.csv --runs "2026-10-*"
   ```
3. Dry-run a small live slice (`$env:LIMIT="50"; $env:APPLY="0"`) before a full run.

## Data loaders

| Script | Loads | Cadence |
|---|---|---|
| `scripts/load_fcc_uls_microwave.py` | FCC ULS microwave locations → Azure SQL (`sql/fcc_uls_microwave.sql`) | Weekly; `--dry-run` first |
| `scripts/load_overture_buildings.py sites\|plan\|extract\|load` | Overture building outlines within 100 m of every pin → `dbo.OvertureBuilding` (`sql/overture_buildings.sql`) | After each Overture release (~60-day retention) |
| `scripts/load_enrichment_metrics.py` | Rebuilds / backfills the KPI tables (`sql/enrichment_metrics.sql`) | As needed; never during a classify run |
| `scripts/build_nearmap_priority.py` | Priority CSV for `--priority-csv` from earlier run folders | Before a budgeted Nearmap run |

KPIs land in Azure SQL (`dbo.EnrichmentRun`, `dbo.EnrichmentSiteOutcome`, `vEnrichmentKpis`) during `APPLY=1` runs; `METRICS_SQL=0` skips them when SQL is unavailable. Details: [docs/enrichment-metrics.md](docs/enrichment-metrics.md).

## Layout

```
enrichment/   queue → prepare (geocode, registry, footprints) → classify → write gates → Salesforce apply → metrics
              __main__ (single run), lanes (parallel runs), connectx_audit, bucketing (write gates),
              naip_classify (imagery cascade), pipeline, sf_ops, budget, signals/
classifier/   model prompts and calls (asset_classifier, llm, prompts), imagery (NAIP, Nearmap, budget),
              views, evidence, sources/ (Mapillary)
salesforce/   auth and Site_Type picklist mapping
scripts/      loaders, eval and replay, run status, config reference generator
sql/          Azure SQL DDL
docs/         ADRs, configuration reference, metrics schema
```
