# WI / DC Pipeline Status
**July 23, 2026**

One campaign. Scope: **all WI assets** + **the DC subset that went through the pipeline**. Remaining work: **`dc_assets_next` only**.

---

## 1. What ran

| Scope | Count |
|---|---:|
| WI full list (`all_WI_assets.csv`) | **197** |
| DC subset that entered the pipeline | **26** |
| **Total that ran** | **223** |

*(DC full master list is 955; only this subset was processed. `dc_assets_next` holds the rest.)*

---

## 2. Marked duplicate (already in Salesforce)

| | Count |
|---|---:|
| Pipeline SF-dedupe duplicates (WI + DC) | **111** |
| Additional hits-file rows already in SF (not net-new) | **5** |

Those **5** (all WI):

- 1306 E Meinecke Av, Milwaukee 53212  
- 1351 W North Av, Milwaukee 53205  
- 235 W Galena St, Milwaukee 53212  
- 2400 E Bradford Av, Milwaukee 53211  
- 841 N Broadway, Milwaukee 53202  

---

## 3. Classified (imagery × type)

**130** unique sites with a model site type.

| Imagery | Rooftop | Tower | Other | Unclear | Total |
|---|---:|---:|---:|---:|---:|
| NAIP | 15 | 13 | 0 | 0 | **28** |
| Nearmap vertical | 17 | 8 | 0 | 0 | **25** |
| Nearmap obliques | 59 | 8 | 8 | 1 | **76** |
| Zoom | 1 | 0 | 0 | 0 | **1** |
| **Total** | **92** | **29** | **8** | **1** | **130** |

| Site type | Count |  | Imagery stage | Count |
|---|---:|---|---|---:|
| Rooftop | **92** |  | Nearmap obliques | **76** |
| Tower | **29** |  | NAIP | **28** |
| Other | **8** |  | Nearmap vertical | **25** |
| Unclear | **1** |  | Zoom | **1** |

---

## 4. Nearmap image pulls (for usage / billing)

These are **individual Nearmap images fetched** (not “sites”). Vertical = top-down; oblique = N/E/S/W.

### Full campaign (WI + DC subset chips on disk)

| Nearmap view | Images |
|---|---:|
| Top-down (Vert) | **113** |
| Oblique (N/E/S/W) | **389** |
| **Total Nearmap images** | **502** |
| Approx. on-disk size | **~217 MB** |

### Batch that matches **47.03 MB** usage

On-disk Nearmap chips for the main WI classify/upload batch (`orchestrator_2026-07-22_211618`) are **~46.3 MB** — lines up with **47.03 MB** billed (small delta is normal for transfer vs stored size).

| Nearmap view | Images | Approx. size |
|---|---:|---:|
| Top-down (Vert) | **30** |  |
| Oblique (N/E/S/W) | **44** |  |
| **Total** | **74** | **~46.3 MB** (≈ **47.03 MB** usage) |

Same calendar day, rest of campaign Nearmap chips (other WI + DC pulls): **~13.2 MB** more on disk if your usage meter is day-total rather than that batch only (**~59.6 MB** day total on disk).

---

## 5. Successful Salesforce loads

| | Count |
|---|---:|
| Successful creates from hits file (excluding the 5 already-in-SF dups) | **60** |
| Additional successful create (1000 Independence Ave SW, DC) | **+1** |
| **Successful loads from this campaign** | **61** |
| Salesforce export inventory | **149** (WI 131 · DC 18) |

Export inventory is larger than campaign loads because SF also holds sites already in the org (duplicates / prior inventory).

Upload path fix verified: creates succeed without invalid `Permit_Metadata__c`.

---

## 6. What’s left

**Only** `data/dc_assets_next.csv` (**930** rows).

---

## Email blurb

We ran the full WI set (197) plus a 26-site DC subset. **111** were SF duplicates in-pipeline (plus **5** already-in-SF on the hits file). **130** sites classified — **92 rooftop / 29 tower / 8 other / 1 unclear**; imagery mostly Nearmap obliques (76), then NAIP (28) and Nearmap vertical (25). Nearmap usage of **47.03 MB** matches **30 top-down + 44 oblique = 74 images** (~46.3 MB on disk). **61** successful Salesforce loads from this campaign; org export shows **149** Site records. Remaining queue: **DC next only**.
