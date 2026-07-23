# WI / DC Pipeline Status
**July 23, 2026**

One campaign across June + July work. Salesforce load (yesterday) is the end state. Remaining queue: **`dc_assets_next` only** (**930** DC).

All Milwaukee WI inventory in this campaign was run through Salesforce dedupe; outcomes were either already-in-SF or net-new creates.

---

## Salesforce load (end state — loaded yesterday)

**150** Site__c records loaded yesterday with `Carrier_Leasing_Source__c = 'JF_PermitScraping_jul26'`.

| Source of create | WI | DC | Total |
|---|---:|---:|---:|
| June lineage (`sf_upload_WI_DC_successful_hits`) | 42 | 18 | **60** |
| July orchestrator (7/22) | 89 | 1 | **90** |
| **Loaded** | **131** | **19** | **150** |

**Note (July 90 loaded vs 62 classified):** July SF creates exceed finished classify rows because the upload CSV was built from dedupe **net-new**, not only from sites that finished imagery/AI in that run.

---

## June run (approximate funnel)

June was classify-first / upload later — SF dedupe was not as cleanly instrumented as July.

| State | Input (classify batches) | Classified (uploadable) |
|---|---:|---:|
| **WI** (`2026-06-12`) | **47** | **47** |
| **DC** (`001-010` + `011-020`) | **21** | **18** |
| **June total** | **68** | **65** |

**3** DC classify inputs did not yield an uploadable classified hit (blank / non-usable outcomes) — hence **21** in → **18** classified on the upload path.

Upload-side: June hits file **65** rows (**47 WI / 18 DC**); **5 WI** were already in Salesforce (not net-new); **60** landed in yesterday’s SF success set.

June did pull Nearmap historically; use **July** figures below for current billing increments.

---

## July run (7/22 orchestrator) — accurate for billing

| State | Input | Classified |
|---|---:|---:|
| **WI** | **151** | **61** |
| **DC** | **5** | **1** |
| **July total** | **156** | **62** |

Non-classified remainder (**156 − 62 = 94**) is **not** all duplicates: geocode failures, incomplete dedupe/classify, and Salesforce **insert duplicate failures** (already in SF when create was attempted).

July SF creates from this run path: **90** (see note above on why **90 > 62**).

### July classification by imagery tier (billing-relevant)

| Imagery tier | WI | DC | Total sites |
|---|---:|---:|---:|
| **NAIP only** (no Nearmap spend) | **25** | **0** | **25** |
| **Nearmap vertical** | **21** | **1** | **22** |
| **Nearmap oblique** (full N/E/S/W) | **14** | **0** | **14** |
| **Zoom** | **1** | **0** | **1** |
| **Total classified** | **61** | **1** | **62** |

### July Nearmap image pull (API / MB)

| | Sites | Vert images | Oblique images | Total images |
|---|---:|---:|---:|---:|
| WI | 36 | 36 | 60 | **96** |
| DC | 1 | 1 | 0 | **1** |
| **July total** | **37** | **37** | **60** | **97** |

**Billable flag for 7/22:** **47.03 MB** on the main WI escalation batch — **30 sites / 74 images** (**30** vert + **44** oblique). Pipeline is NAIP-first; only sites that still needed better imagery escalated to Nearmap, then obliques if still needed. In that batch, **11** classified sites stayed NAIP-only and incurred no Nearmap.

---

## What’s next / what’s left

- **Next:** Intermediary step before imagery/AI — cross-reference **FCC** + **TowerSource** in **MSSQL** so known assets are filtered earlier.
- **Left:** `data/dc_assets_next.csv` — **930** DC sites pending classification and upload.

---

## Email blurb

Hi all,

Quick status on the WI/DC campaign. The work spanned two runs — a June classify/upload lineage and a July 22 orchestrator run — and both were loaded into Salesforce yesterday. All Milwaukee WI inventory in this campaign was run through Salesforce dedupe; outcomes were either already-in-SF or net-new creates. **930** DC sites are pending classification and upload.

**Salesforce (end state, loaded yesterday)**

**150** Site__c records were loaded yesterday with `Carrier_Leasing_Source__c = 'JF_PermitScraping_jul26'`, broken down as:

| Source | WI | DC | Total |
|---|---:|---:|---:|
| June lineage (hits upload) | 42 | 18 | **60** |
| July orchestrator (7/22) | 89 | 1 | **90** |
| **Total loaded** | **131** | **19** | **150** |

July SF creates (**90**) exceed finished classify rows (**62**) because the upload CSV was built from dedupe net-new, not only from sites that finished imagery/AI in that run.

---

**June run** *(funnel is less precise — this was the classify-first era)*

| | WI | DC | Total |
|---|---:|---:|---:|
| Went in (classify batches) | **47** | **21** | **68** |
| Classified (uploadable) | **47** | **18** | **65** |

**3** DC inputs did not yield an uploadable classified hit. Salesforce dedupe wasn’t instrumented the same way in June as in July. On the upload side, the June hits file had **65** rows (**47 WI / 18 DC**); **5** were already in Salesforce (not net-new), leaving **60** in yesterday’s success set. June did pull Nearmap historically; use the July numbers below for current billing increments.

---

**July run (7/22)**

| | WI | DC | Total |
|---|---:|---:|---:|
| Went into orchestrator | **151** | **5** | **156** |
| Classified | **61** | **1** | **62** |

One note: the non-classified remainder (**94**) is not all duplicates. It’s mostly geocode failures, incomplete dedupe/classify outcomes, and Salesforce insert duplicate failures (already in SF when create was attempted).

**July imagery / billing increments** (classified sites by final tier):

| Tier | Sites | Meaning for spend |
|---|---:|---|
| NAIP only | **25** | No Nearmap API |
| Nearmap vertical | **22** | Top-down Nearmap only |
| Nearmap oblique | **14** | Full N/E/S/W plus vertical |
| Zoom | **1** | Extra zoom stage |
| **Total classified** | **62** | |

Nearmap images actually pulled yesterday: **37 sites / 97 images** (**37** vertical + **60** oblique).

**Nearmap API spend to flag:** **47.03 MB** was billed on the main WI escalation batch — **30 sites / 74 images** (**30** vertical + **44** oblique). The pipeline is NAIP-first; only sites that still needed better imagery escalated to Nearmap, then to obliques if still needed. In that batch, **11** classified sites stayed NAIP-only and incurred no Nearmap cost.

---

**What’s next**

I’m building an intermediary step ahead of imagery/AI that cross-references candidates against **FCC** and **TowerSource** in our **MSSQL** server, so known assets get filtered out earlier.

**What’s left**

**930** DC sites pending classification and upload.

Thanks,
