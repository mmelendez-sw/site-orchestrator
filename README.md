# Site Orchestrator

Salesforce enrichment: pull blank `Site_Type__c` sites, snap to FCC/TowerSource, classify from NAIP + optional Nearmap with Gemini/Claude (OSM prefilter before paid imagery), then write each qualifying site back to Salesforce as soon as it finishes. An end-of-run sweep applies anything still pending.

There is no upload-template or CSV-import step. Run CSVs under `../site-orchestrator-data/runs/` are an audit log.

```
python -m enrichment
```

Set `APPLY=0` to classify and write CSVs without Salesforce updates. Optional env: `STATES`, `STAGES`, `LIMIT`, `OFFSET`, `SKIP_FROM`, `IDS`, `CARRIER_LIKE`, `METRO_CLASSIFICATION`, `LLM_CLASSIFIED`, `RUN_DIR`, `VERBOSE`, `METRICS_SQL`, `DEQUEUE_HOLDOUTS`, `DB_ONLY`, `CONFIRM_ROOFTOP`, `CLASSIFY_WORKERS`, `APPLY_BATCH_SIZE`. `SKIP_FROM=sql` skips every Id a live run already recorded in Azure SQL (combine with run folders / dates by comma). The queue defaults to `Stage__c = 'Outreach - Verified'` with no `LIMIT`. Set `STAGES` to a comma-separated picklist list to widen it.

`CARRIER_LIKE` is the `Carrier_Leasing_Source__c` LIKE needle (unset = no carrier filter). Set `CARRIER_LIKE=NFL` to restrict to NFL sources. `METRO_CLASSIFICATION` is an exact `Metro_Classification__c` match (unset = no metro filter; e.g. `Major NFL Metro`). `OWNERS` is a comma list for `Owner__c IN (...)` (unset = any owner). Enrichment does not write those fields. The queue defaults to `LLM_Classified__c = false`; set `LLM_CLASSIFIED=1` only to re-pull already-flagged rows.

**DB-only (no Nearmap):** `DB_ONLY=1` walks blank-`Site_Type` sites in **New/Unreviewed, Enhanced/Unreviewed, Outreach, Outreach - Verified, Marketing**, any owner. Successful writes are `LLM_Classified=true` (no `LLM_Holdout`). Unique FCC/TowerSource hits also write site type and coords. Misses stay blank type and classified true until you flip them (`LLM_CLASSIFIED=1`, then set classified false). If a Salesforce update fails (duplicates, API errors), the apply retries once with `LLM_Classified=false` and `LLM_Holdout=true` so the site leaves the queue. `SKIP_FROM` still skips Ids already in prior run CSVs. Change stages with `STAGES` or `LEAD_STAGES`.

```powershell
$env:DB_ONLY="1"
$env:METRO_CLASSIFICATION="none"
$env:STAGES="New/Unreviewed,Enhanced/Unreviewed,Outreach,Outreach - Verified,Marketing"
$env:LIMIT="200"
$env:SKIP_FROM="2026-09-09"
$env:APPLY="0"
$env:VERBOSE="1"
python -m enrichment
```

Then set `APPLY=1` for live Salesforce writes. `Working-Connected` / `Qualified (Converted)` stay excluded unless listed in `STAGES`.

Leadership KPIs land in Azure SQL (`dbo.EnrichmentRun` / `dbo.EnrichmentSiteOutcome`) during `APPLY=1` runs: the run header at start, each site row as that site finishes, and a full reconcile at the end. **UniqueSites** is every distinct Salesforce Id a live run processed — applied, held out, DB-only miss, failed apply, or error. **WrittenSites** is the subset Salesforce accepted a site-type/coords write for (`sf_update_status=updated`, including rooftop-confirm applies). Retries of the same Id count once (latest outcome wins). `APPLY=0` dry-runs never touch the cumulative KPIs or `kpis.json`. Details: [docs/enrichment-metrics.md](docs/enrichment-metrics.md).

**NAIP rooftop confirm:** `CONFIRM_ROOFTOP=1` pulls existing `Site_Type=Rooftop` (not blank type), classifies NAIP + Gemini for **building-roof presence** (not cellular gear), and writes `Site_Type` + `LLM_Classified=true` when NAIP labels rooftop. HVAC-only or empty roofs still confirm. Inconclusive rows are left unchanged. No Nearmap, no Claude, no cell-gear bar. Successful applies count as rooftop SF writes / UniqueSites. Default stages include Working-Connected; set `CARRIER_LIKE` as needed. Uncomment `LIMIT` in `.env` only to cap a slice. Dry-run with `APPLY=0` first.

```powershell
$env:CONFIRM_ROOFTOP="1"
$env:CARRIER_LIKE="ConnectX"
$env:METRO_CLASSIFICATION="none"
$env:OWNERS="none"
$env:APPLY="0"
$env:VERBOSE="1"
python -m enrichment
```

Salesforce apply is stage 4/4. If you Ctrl+C during classify, push the paused CSV (no Gemini rerun):

```powershell
$env:CONFIRM_ROOFTOP="1"
$env:APPLY="1"
$env:APPLY_EXISTING="1"
$env:RUN_DIR="C:\Users\Mmelendez\Codebases\site-orchestrator-data\runs\2026-09-17_130656_sf_enrichment"
$env:VERBOSE="1"
python -m enrichment
```

**ConnectX rooftop audit:** `CONNECTX_AUDIT=1` re-checks ConnectX rooftops that are still owned by an individual rep. These are `Site_Type=Rooftop` rows whose `OwnerId` is not the Site Acquisition Team (`0053l00000G05h9AAB`) and whose stage is New/Unreviewed, Enhanced/Unreviewed, Outreach or Outreach - Verified. The `LLM_Classified` flag is ignored. These sites run the full FCC/TowerSource → NAIP → Nearmap Vert + obliques → Claude path, and each finished site gets one verdict:

| Verdict | When | Salesforce write |
|---|---|---|
| `confirmed` | Normal write gates passed (unique FCC/TowerSource hit, or Nearmap obliques + dual-model cell) | Usual Site_Type / coords / Verified_Site_Source. Owner and stage stay the same. |
| `no_asset` | Nearmap obliques reviewed, no model saw gear, confident and unhedged call, no stealth host (steeple, chimney, screen wall…), no FCC/TowerSource record within `AUDIT_DB_VETO_M` (100 m) | `OwnerId` = Site Acquisition Team, `Stage__c` = Unqualified, `Unqualified_Reason__c` = No Site/Decommissioned, `Other_Unqualified_Reason__c` = audit note, `Unqualified_Date__c` = today |
| `inconclusive` | Anything else: NAIP only, no Nearmap coverage, budget stop, gear claimed but disputed, weak call | None. The site stays with its rep. |

The detail CSV columns `audit_verdict` and `audit_reason` record each call. Live runs write to `runs/<stamp>_connectx_audit`, and `APPLY=0` runs write to `_connectx_audit_dryrun`. Each new audit skips Ids an earlier live audit already decided. Set `AUDIT_RETRY_INCONCLUSIVE=1` to retry inconclusive sites; budget stops and errors are always retried. Scope a night with `OWNERS` (rep names), `STATES` or `LIMIT`. Owner override: `SITE_ACQ_OWNER_ID`. The audit uses roughly 1.1 MB of Nearmap per site, so check `NEARMAP_MONTHLY_BUDGET_MB` before a large slice.

```powershell
$env:CONNECTX_AUDIT="1"
$env:OWNERS="Jeremy Scott"   # optional: one rep per night
$env:LIMIT="200"
$env:APPLY="0"               # dry run first; review audit_verdict in the detail CSV
$env:VERBOSE="1"
python -m enrichment
```

To push a reviewed dry run without re-buying imagery, set `APPLY=1`, `APPLY_EXISTING=1`, `CONNECTX_AUDIT=1` and `RUN_DIR` to the `_connectx_audit_dryrun` folder.

### ConnectX reconciliation (pool audit → rep audit → hot swap)

1. **Verify the pool.** `CONNECTX_AUDIT=1 AUDIT_POOL=1` audits ConnectX rooftops owned by the Site Acquisition Team. Confirmed rooftops get the NearMap verification and stay in the pool. No-asset sites are set to Unqualified and stay in the pool.
2. **Verify rep books (not worked only).** `CONNECTX_AUDIT=1 STAGES=New/Unreviewed AUDIT_UNQUALIFY_OWNER=0056O00000EpUOgQAN` audits rep-owned New/Unreviewed rooftops. No-asset sites are set to Unqualified and reassigned to Matt Melendez. Confirmed sites stay with the rep.
3. **Hot swap.** `python -m enrichment.reconcile_swaps` gives each rep one verified pool rooftop for every site step 2 took away, preferring the same state and falling back to any state. It re-checks the live Salesforce state first. Without `SWAP_APPLY=1` it only writes a plan. A swap sets `OwnerId` to the rep and `Site_Assignment_Date__c` to today, and `swaps/swap_ledger.csv` keeps any site from being used twice. Optional: `SWAP_LIMIT`, `SWAP_REPS`.

```powershell
# 1. pool
$env:CONNECTX_AUDIT="1"; $env:AUDIT_POOL="1"; $env:LIMIT="200"; python -m enrichment
# 2. reps (new terminal, or clear AUDIT_POOL first)
$env:CONNECTX_AUDIT="1"; $env:STAGES="New/Unreviewed"; $env:AUDIT_UNQUALIFY_OWNER="0056O00000EpUOgQAN"; $env:LIMIT="200"; python -m enrichment
# 3. swaps: review the plan, then apply
python -m enrichment.reconcile_swaps
$env:SWAP_APPLY="1"; python -m enrichment.reconcile_swaps
```

## Layout

```
site-orchestrator/
├── enrichment/     # queue → prepare (geocode + bulk proximity) → classify → apply → metrics
├── classifier/     # decision gates (asset_classifier), prompts, imagery, llm, views, evidence
├── salesforce/     # auth + Site_Type picklist mapping
├── sql/            # enrichment metrics DDL (Symphony_dev)
├── scripts/        # metrics loader/backfill, decision replay
├── docs/           # metrics schema and queries
├── envutil.py      # typed env readers
├── paths.py        # sibling data folder
└── requirements.txt
```

CSVs, `runs/`, and `chips/` live in **`../site-orchestrator-data`** (not in git). Override with `SITE_ORCHESTRATOR_DATA`.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # fill in credentials
```

Also install the ODBC Driver 18 for SQL Server. For tests and lint: `pip install -r requirements-dev.txt`, then `python -m pytest -q` (offline — no paid APIs, Salesforce, or SQL). CI runs the same on every push (`.github/workflows/tests.yml`). Azure SQL uses Entra token auth (`az login`); `pyodbc` and `azure-identity` are in `requirements.txt`.

## Classify path

1. **NAIP + Gemini** — always, unless a unique FCC/TowerSource hit ≤ 25 m skips imagery.
2. **OSM Overpass** — cheap prefilter before Nearmap when the NAIP pass is inconclusive.
3. **Nearmap** vert + obliques — rooftops and towers that still need high-res sides.
4. **Claude** — dual-model cell confirm (skipped for high-conf Gemini towers ≥ 0.85, and below 0.7 site confidence).

### Throughput and pacing

One process classifies `CLASSIFY_WORKERS` sites at once (default 10). Pins within `PIN_CLUSTER_M` of each other stay in one worker so nearby-pin reuse still saves imagery. Every Gemini / Claude call waits on a shared limiter — `GEMINI_RPM` (default 120) and `CLAUDE_RPM` (default 50) — and a 429/503 pauses all workers together. This replaces the old fixed `GEMINI_DELAY_S` sleep after each site (`GEMINI_DELAY_S` / `CLAUDE_DELAY_S` are ignored now). Run **one** terminal and raise `CLASSIFY_WORKERS` instead of opening more; separate processes do not share the limiter. Every 429 prints a `WARNING Gemini 429 … all workers pause` line; if those repeat, lower `GEMINI_RPM` (e.g. 90 or 60).

With `VERBOSE=1` and more than one worker, each site's steps print as one block when that site finishes (tagged `[index/total Id]`), followed by its result line. `CLASSIFY_WORKERS=1` restores live step-by-step output.

Scaling: `CLASSIFY_WORKERS` goes up to 32, but the ceiling is model pacing — roughly `GEMINI_RPM` ÷ Gemini calls per site (typically 2–5) sites per minute. More workers only help once `GEMINI_RPM` is raised to match, up to your Gemini project quota. Each worker also opens up to `NEARMAP_TILE_WORKERS` tile downloads at once (default 4, so 40 concurrent at 10 workers).

Before classifying, the run geocodes the whole queue in Census batch requests (cached in `../site-orchestrator-data/cache/census_geocode.jsonl`) and looks up FCC/TowerSource for all pins in a few temp-table joins instead of two to four queries per site. If the bulk lookup fails, sites fall back to per-site queries automatically.

Salesforce writes, Azure SQL, and the detail CSV all stay on the main thread. The first Ctrl+C finishes in-flight sites and flushes pending writes; a second aborts. Salesforce writes go out in sObject Collections batches of `APPLY_BATCH_SIZE` (default 25, max 200) or after `APPLY_FLUSH_S` (default 60 s), whichever comes first; `APPLY_BATCH_SIZE=1` writes each site as it finishes. Every finished site is in `enrichment_detail.csv` (status `pending`) before its batch is sent, so after a hard crash `APPLY_EXISTING=1` with that `RUN_DIR` pushes whatever had not gone out.

Imagery is cached under `../site-orchestrator-data/cache/`: NAIP chips per STAC item + point, Nearmap tiles per survey capture date (a new survey refetches). Reruns, wide AOIs, re-centers, and neighbors reuse pixels instead of re-downloading. Set `IMAGERY_CACHE=0` to disable; check your Nearmap agreement on how long tiles may be retained. Do not run `load_enrichment_metrics.py` during a classify run. Set `METRICS_SQL=0` if Azure SQL is dropping; reload KPIs later.

### Nearmap spend (4 GB/month shared with ICEMAN)

Every Nearmap tile is metered: billed bytes, tiles, cache hits, and the stage that bought it (`pack`, `oblique_extra`, `wide`, `second`, `recenter`). Per site it lands in `enrichment_detail.csv` (`nearmap_bytes`, `nearmap_spend`) and Azure SQL (`NearmapBytes`); per run and cumulatively as `nearmap_mb` / `nearmap_mb_per_enriched` (`NearmapMB`, `NearmapMBPerEnriched` in `vEnrichmentKpis`).

Purchase rules that keep bytes down:

| Purchase | Tiles | When |
|---|---|---|
| Vert + first oblique (100 m, z20) | ~40 | Sites that need Nearmap |
| Remaining obliques | ~20 each | Only when Vert + first oblique did not lock the call (`NEARMAP_STAGGER_OBLIQUES=1`) |
| Wide host scout (250 m, **z19 Vert only**) | ~25 | Rooftop with cell unconfirmed (was ~270 for the full 250 m pack; `NEARMAP_WIDE_SCOUT=full` restores it) |
| Re-center pack (100 m) | ~60 | Scout found a candidate off the pin |
| Second pack at the Census point | ~60 | Pin and address disagree |

The monthly guard (`NEARMAP_MONTHLY_BUDGET_MB`, default 1500) sums this month's bytes from `metrics/nearmap_usage.jsonl` (dry runs included) plus `NEARMAP_PRIOR_USE_MB`. At `NEARMAP_BUDGET_SOFT_PCT` (90%) the optional buys stop; at 100% all Nearmap stops and sites that needed it hold out as `nearmap_budget` — never dequeued, so the next month's run picks them up. The start banner prints `Nearmap budget: N of M MB used this month`. ICEMAN usage is not visible here: size the budget to this pipeline's share.

### Checking a rule change

Before shipping a threshold or gate change, replay recent classifications through the new rules (no imagery, models, or Salesforce):

```powershell
python scripts/replay_decisions.py 2026-09
```

It prints every site whose bucket / holdout reason / Site_Type would change and exits 1 if any did.

Re-score holdouts on already-purchased Nearmap JPEGs (no new Nearmap fetch; Gemini and Claude still run). Dry-run a slice first:

```powershell
$env:RERUN_HOLDOUTS_FROM="2026-09-03"
$env:REUSE_CHIPS_FROM="2026-09-03"
$env:LIMIT="50"
$env:APPLY="0"
$env:VERBOSE="1"
python -m enrichment
```

Sites with no saved chips return `error=no_saved_chips`. Then `APPLY=1` if the holdout mix looks right.

## What it writes

- **Towers:** Gemini tower + cell at site confidence ≥ 0.85 auto-apply (imagery-only allowed). Claude can confirm weaker towers.
- **Rooftops:** Nearmap obliques + dual-model agreement, or NAIP-only when site and cell confidence ≥ 0.95 with named gear, unhedged evidence, and an asset box. Otherwise holdout and dequeue (`LLM_Classified=false`, `LLM_Holdout=true`).
- Unique FCC/TowerSource hit ≤ 25 m skips imagery and still updates coords + verified source.
