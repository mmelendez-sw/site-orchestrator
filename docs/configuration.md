# Configuration reference

Every environment variable ICEMAN reads, generated from the source by
`python scripts/gen_config_reference.py` (CI fails when this file is stale).
Values come from the process environment or `.env` (see `.env.example` for the
settings operators normally touch; everything else is a tuning knob with a
safe default). `python -m enrichment.lanes` sets its own per-batch defaults
(`LANE_DEFAULT_ENV` in enrichment/lanes.py, e.g. `APPLY_BATCH_SIZE=10`) and
splits `--gemini-rpm-total` (default `GEMINI_RPM` or 60) and
`--claude-rpm-total` (default `CLAUDE_RPM` or 50) across lanes.

Booleans accept `1` / `true` / `yes` (case-insensitive); anything else is off.
Blank or unparsable numbers fall back to the default. "computed" means the
default is an expression or module constant, and "unset" means the read has
no inline default (the code decides what absence means); see the file.

| Variable | Default | Read in |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | enrichment/naip_classify.py |
| `APPLY` | on | enrichment/__main__.py |
| `APPLY_BATCH_SIZE` | `25` | enrichment/pipeline.py |
| `APPLY_EXISTING` | off | enrichment/__main__.py |
| `APPLY_FLUSH_S` | `60` | enrichment/pipeline.py |
| `ASSET_OFFSET_LEEWAY_M` | `25` | enrichment/constants.py |
| `AUDIT_ASSIGN_TO` | unset | enrichment/__main__.py |
| `AUDIT_CONFIRMED_OWNER` | unset | enrichment/connectx_audit.py |
| `AUDIT_DB_VETO_M` | `100` | enrichment/connectx_audit.py |
| `AUDIT_EMPTY_SITE_CONF` | `0.7` | enrichment/connectx_audit.py |
| `AUDIT_HOLDOUT_OWNER` | unset | enrichment/connectx_audit.py |
| `AUDIT_NO_GEAR_CELL_CONF` | `0.75` | enrichment/connectx_audit.py |
| `AUDIT_POOL` | off | enrichment/__main__.py |
| `AUDIT_RETRY_INCONCLUSIVE` | off | enrichment/__main__.py, enrichment/pipeline.py |
| `AUDIT_UNQUALIFY_OWNER` | unset | enrichment/connectx_audit.py |
| `AUTO_SKIP_CLASSIFY` | on | enrichment/cost_policy.py |
| `AUTO_SKIP_ON_STRUCTURE_M` | `5` | enrichment/constants.py |
| `AZURE_SQL_ALLOW_INTERACTIVE` | unset | enrichment/mssql.py |
| `AZURE_SQL_DATABASE` | unset | enrichment/mssql.py |
| `AZURE_SQL_DRIVER` | unset | enrichment/mssql.py |
| `AZURE_SQL_ODBC_AUTHENTICATION` | unset | enrichment/mssql.py |
| `AZURE_SQL_PWD` | unset | enrichment/mssql.py |
| `AZURE_SQL_SERVER` | unset | enrichment/metrics_store.py, enrichment/mssql.py |
| `AZURE_SQL_UID` | unset | enrichment/mssql.py |
| `AZURE_SQL_WAKE_RETRIES` | unset | enrichment/mssql.py |
| `AZURE_SQL_WAKE_WAIT_S` | unset | enrichment/mssql.py |
| `BIFURCATED_AI` | on | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `CARRIER_LIKE` | unset | enrichment/__main__.py |
| `CHIP_SIZE_M` | `300` | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `CLASSIFY_WORKERS` | `10` | enrichment/pipeline.py |
| `CLAUDE_CROP_MODEL` | `claude-haiku-4-5-20251001` | classifier/asset_classifier.py |
| `CLAUDE_ESCALATE_MIN_SITE_CONF` | `0.6` | classifier/asset_classifier.py |
| `CLAUDE_ESCALATION_MODEL` | `claude-sonnet-4-6` | classifier/asset_classifier.py |
| `CLAUDE_MODELS` | computed | classifier/llm.py |
| `CLAUDE_RPM` | `50` | classifier/llm.py |
| `CONFIRM_CONSISTENCY` | on | enrichment/pipeline.py |
| `CONFIRM_EXISTING` | off | enrichment/__main__.py |
| `CONFIRM_ROOFTOP` | off | enrichment/__main__.py |
| `CONNECTX_AUDIT` | off | enrichment/__main__.py |
| `DB_ONLY` | off | enrichment/__main__.py |
| `DEQUEUE_HOLDOUTS` | computed | enrichment/__main__.py |
| `DISPUTE_EXTRA_OBLIQUES` | on | enrichment/naip_classify.py |
| `FCC_ULS_MICRO_URL` | `https://data.fcc.gov/download/pub/uls/complete/l_micro.zip` | scripts/load_fcc_uls_microwave.py |
| `FOOTPRINT_ADDRESS_MAX_M` | `150.0` | enrichment/footprints.py |
| `FOOTPRINT_BIG_M2` | `10000.0` | enrichment/footprints.py |
| `FOOTPRINT_MAX_SHIFT_M` | `30.0` | enrichment/footprints.py |
| `FOOTPRINT_PIN_CHECK` | on | enrichment/footprints.py |
| `FOOTPRINT_SNAP_MAX_M` | `60.0` | enrichment/footprints.py |
| `FORCE_ROOF_SCOUT` | off | enrichment/naip_classify.py |
| `GEMINI_API_KEY` | unset | enrichment/naip_classify.py |
| `GEMINI_CLAUDE_BOX_IOU` | `0.2` | classifier/asset_classifier.py |
| `GEMINI_FALLBACK_AFTER` | `2` | classifier/asset_classifier.py |
| `GEMINI_FALLBACK_COOLDOWN_S` | `300` | classifier/asset_classifier.py |
| `GEMINI_FALLBACK_MODEL` | unset | classifier/asset_classifier.py |
| `GEMINI_FALLBACK_RETRIES` | `3` | classifier/asset_classifier.py |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `GEMINI_ONLY` | off | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `GEMINI_RETRIES` | `6` | classifier/llm.py |
| `GEMINI_RETRY_BASE_S` | `20` | classifier/llm.py |
| `GEMINI_RPM` | `120` | classifier/llm.py |
| `GEMINI_SCREEN_MODEL` | `gemini-3.5-flash-lite` | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `GEMINI_SCREEN_THINKING_LEVEL` | unset | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `GEMINI_SOFT_KEEP_CELL_CONF` | `0.85` | classifier/asset_classifier.py |
| `GEMINI_SOLO_CELL_CONF` | `0.85` | classifier/asset_classifier.py, enrichment/constants.py, enrichment/naip_classify.py |
| `GEMINI_THINKING_BUDGET` | unset | classifier/asset_classifier.py |
| `GEMINI_THINKING_LEVEL` | unset | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `GEMINI_TOWER_LOCK_CELL_CONF` | `0.8` | classifier/asset_classifier.py, enrichment/bucketing.py |
| `GEOCODE_ADDRESS` | on | enrichment/geo.py |
| `GEOCODE_BATCH_SIZE` | `1000` | enrichment/geo.py |
| `GEOCODE_BATCH_TIMEOUT_S` | `300` | enrichment/geo.py |
| `GEOCODE_CACHE` | on | enrichment/geo.py |
| `GEOCODE_TIMEOUT_S` | `5` | enrichment/geo.py |
| `IDS` | unset | enrichment/__main__.py |
| `IMAGERY_CACHE` | on | classifier/imagery.py, classifier/sources/base.py, enrichment/osm_prefilter.py |
| `LEAD_STAGES` | unset | enrichment/__main__.py |
| `LIMIT` | unset | enrichment/__main__.py, enrichment/metrics.py |
| `LLM_CLASSIFIED` | off | enrichment/__main__.py |
| `MAPILLARY_ACCESS_TOKEN` | unset | classifier/sources/mapillary.py |
| `MAPILLARY_CROP` | `1` | classifier/sources/mapillary.py |
| `MAPILLARY_CROP_TOP` | `0.7` | classifier/sources/mapillary.py |
| `MAPILLARY_CROP_WIDTH` | `0.55` | classifier/sources/mapillary.py |
| `MAPILLARY_HFOV` | `80.0` | classifier/sources/mapillary.py |
| `MAPILLARY_MAX_HEADING_DIFF` | `50.0` | classifier/sources/mapillary.py |
| `MAPILLARY_MAX_VIEWS` | `3` | classifier/sources/mapillary.py |
| `MAPILLARY_RADIUS_M` | `100.0` | classifier/sources/mapillary.py |
| `MAPILLARY_SEARCH_CACHE_DAYS` | `30.0` | classifier/sources/mapillary.py |
| `METRICS_SQL` | on | enrichment/metrics_store.py |
| `METRO_CLASSIFICATION` | unset | enrichment/__main__.py |
| `MIN_IMAGERY_ONLY_CELL_CONFIDENCE` | `0.8` | enrichment/constants.py |
| `MIN_IMAGERY_ONLY_SITE_CONFIDENCE` | `0.7` | enrichment/constants.py |
| `MIN_IMAGERY_ONLY_SITE_CONFIDENCE_AGREE` | `0.65` | enrichment/constants.py |
| `MODEL_IMAGE_MAX_PX` | `768` | classifier/views.py |
| `MODEL_MAX_OBLIQUES` | `2` | classifier/views.py |
| `MODEL_MAX_STREET_VIEWS` | `3` | classifier/views.py |
| `NAIP_AGE_HIGH_CONF_OVERRIDE` | `0.85` | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `NAIP_MAX_AGE_YEARS` | `2` | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `NAIP_ONLY` | off | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `NAIP_ROOFTOP_SKIP` | off | classifier/asset_classifier.py |
| `NAIP_WIDE_CHIP_M` | `500` | classifier/asset_classifier.py |
| `NEARMAP_ALWAYS` | off | classifier/asset_classifier.py |
| `NEARMAP_API_KEY` | unset | classifier/imagery.py |
| `NEARMAP_BUDGET_REFRESH_S` | `5` | classifier/imagery.py |
| `NEARMAP_BUDGET_SOFT_PCT` | `90` | classifier/imagery.py, enrichment/budget.py |
| `NEARMAP_CACHE_ONLY` | on | enrichment/naip_classify.py |
| `NEARMAP_EMPTY_LOCK_CONF` | `0.9` | classifier/asset_classifier.py, enrichment/constants.py, enrichment/naip_classify.py |
| `NEARMAP_MAX_PX` | `1024` | classifier/imagery.py |
| `NEARMAP_MONTHLY_BUDGET_MB` | `1500` | classifier/imagery.py, enrichment/budget.py |
| `NEARMAP_OBLIQUE_VIEWS` | `North,East` | classifier/imagery.py |
| `NEARMAP_OBLIQUE_ZOOM` | `20` | classifier/imagery.py |
| `NEARMAP_PRIOR_USE_MB` | `0` | enrichment/budget.py, enrichment/lanes.py, enrichment/pipeline.py |
| `NEARMAP_SCOUT_ZOOM` | `19` | classifier/imagery.py |
| `NEARMAP_STAGGER_OBLIQUES` | on | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `NEARMAP_TIERED` | on | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `NEARMAP_TILE_CACHE` | on | classifier/imagery.py |
| `NEARMAP_TILE_WORKERS` | `4` | classifier/imagery.py |
| `NEARMAP_VERT_ZOOM` | `20` | classifier/imagery.py |
| `NEARMAP_WIDE_SCOUT` | `vert` | enrichment/naip_classify.py |
| `OFFSET` | unset | enrichment/__main__.py |
| `OPENCELLID_API_KEY` | unset | enrichment/signals/opencellid.py |
| `OPENCELLID_CACHE_DAYS` | `30` | enrichment/signals/opencellid.py |
| `OPENCELLID_RADIUS_M` | `300` | enrichment/signals/opencellid.py |
| `OPENCELLID_RPM` | `30` | enrichment/signals/opencellid.py |
| `OPENCELLID_TIMEOUT_S` | `10` | enrichment/signals/opencellid.py |
| `OPENCELLID_URL` | `https://opencellid.org/cell/getInArea` | enrichment/signals/opencellid.py |
| `OSM_ANTENNA_CACHE_DAYS` | `30` | enrichment/signals/osm_antenna.py |
| `OSM_ANTENNA_RADIUS_M` | `60` | enrichment/signals/osm_antenna.py |
| `OSM_ANTENNA_RPM` | `60` | enrichment/signals/osm_antenna.py |
| `OSM_ANTENNA_STRONG_M` | `30` | enrichment/signals/__init__.py |
| `OSM_ANTENNA_TIMEOUT_S` | computed | enrichment/signals/osm_antenna.py |
| `OSM_PREFILTER` | on | enrichment/osm_prefilter.py |
| `OSM_PREFILTER_CACHE_DAYS` | `90` | enrichment/osm_prefilter.py |
| `OSM_RADIUS_M` | `80` | enrichment/osm_prefilter.py |
| `OSM_TIMEOUT_S` | `20` | enrichment/osm_prefilter.py |
| `OVERPASS_MIRRORS` | computed | enrichment/signals/osm_antenna.py |
| `OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | enrichment/osm_prefilter.py |
| `OWNERS` | unset | enrichment/__main__.py |
| `OWNERS_EXCLUDE` | unset | enrichment/__main__.py |
| `PIN_ADDRESS_MISMATCH_M` | `75` | enrichment/constants.py |
| `PIN_CLUSTER_M` | `25` | enrichment/cost_policy.py |
| `PROXIMITY_ADDRESS_AFFINITY_M` | `100` | enrichment/constants.py |
| `PROXIMITY_AMBIGUITY_GAP_M` | `60` | enrichment/constants.py |
| `PROXIMITY_BULK_CHUNK` | `200` | enrichment/mssql.py |
| `PROXIMITY_CONFIDENT_M` | `25` | enrichment/constants.py |
| `PROXIMITY_MAX_M` | `500` | enrichment/constants.py |
| `PULL_APPLY` | unset | enrichment/reconcile_pull.py |
| `PULL_ASSIGNED_ON` | `2026-09-22` | enrichment/reconcile_pull.py |
| `PULL_LIMIT` | unset | enrichment/reconcile_pull.py |
| `PULL_REPS` | unset | enrichment/reconcile_pull.py |
| `QUEUE_OFFSET` | unset | enrichment/__main__.py |
| `RERUN_HOLDOUTS_FROM` | unset | enrichment/__main__.py |
| `RERUN_SITES_FROM` | unset | enrichment/__main__.py |
| `RERUN_SKIP_APPLIED` | on | enrichment/__main__.py |
| `REUSE_CHIPS_FROM` | unset | enrichment/__main__.py |
| `ROOFTOP_CELL_CONF_MIN` | `0.75` | classifier/asset_classifier.py |
| `ROOFTOP_CERTAIN_CONF` | `0.95` | enrichment/constants.py |
| `ROOFTOP_CROP_CONFIRM` | on | classifier/asset_classifier.py |
| `ROOFTOP_CROP_STRICT` | off | classifier/asset_classifier.py |
| `ROOFTOP_CROP_STRICT_CONF` | `0.8` | classifier/asset_classifier.py |
| `ROOFTOP_HOST_OFFSET_M` | `15` | enrichment/constants.py |
| `RUN_DIR` | unset | enrichment/__main__.py |
| `SAVED_NEARMAP_CHIPS` | on | enrichment/naip_classify.py |
| `SCREEN_IMAGE_MAX_PX` | `768` | classifier/views.py |
| `SF_DOMAIN` | `login` | salesforce/sf_client.py |
| `SF_PASSWORD` | required | salesforce/sf_client.py |
| `SF_SECURITY_TOKEN` | required | salesforce/sf_client.py |
| `SF_USERNAME` | required | salesforce/sf_client.py |
| `SIGNALS` | on | enrichment/signals/__init__.py |
| `SIGNALS_BULK_CHUNK` | `200` | enrichment/signals/uls.py |
| `SIGNALS_SOURCES` | unset | enrichment/signals/__init__.py |
| `SITE_ACQ_OWNER_ID` | unset | enrichment/connectx_audit.py |
| `SITE_ORCHESTRATOR_DATA` | unset | paths.py |
| `SITE_TYPE` | unset | enrichment/__main__.py |
| `SKIP_CLASSIFY` | off | enrichment/__main__.py |
| `SKIP_FROM` | unset | enrichment/__main__.py |
| `STAGES` | unset | enrichment/__main__.py |
| `STATES` | unset | enrichment/__main__.py, enrichment/metrics.py |
| `STREET_CONFIRM_MAX_AGE_YEARS` | `5.0` | classifier/sources/mapillary.py, enrichment/bucketing.py |
| `SUPPLEMENTAL_CAN_CONFIRM` | on | classifier/views.py |
| `SUPPLEMENTAL_IMAGERY` | `mapillary` | classifier/sources/registry.py |
| `SUPPLEMENTAL_TIMEOUT_S` | `15.0` | classifier/sources/base.py |
| `SWAP_APPLY` | unset | enrichment/reconcile_swaps.py |
| `SWAP_BRIDGE` | unset | enrichment/reconcile_swaps.py |
| `SWAP_LIMIT` | unset | enrichment/reconcile_swaps.py |
| `SWAP_REPS` | unset | enrichment/reconcile_swaps.py |
| `TIER_CONF_HIGH` | `0.75` | classifier/asset_classifier.py |
| `TIER_CONF_MEDIUM` | `0.6` | classifier/asset_classifier.py |
| `TOWER_ONLY` | off | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `ULS_RADIUS_M` | `150` | enrichment/signals/uls.py |
| `ULS_STRONG_M` | `30` | enrichment/signals/__init__.py |
| `VERBOSE` | off | enrichment/__main__.py |
| `WIDE_AOI_STAGE` | on | classifier/asset_classifier.py, enrichment/naip_classify.py |
| `ZOOM_MAX_CANDIDATES` | `3` | classifier/views.py |
| `ZOOM_STAGE` | on | classifier/asset_classifier.py, enrichment/naip_classify.py |
