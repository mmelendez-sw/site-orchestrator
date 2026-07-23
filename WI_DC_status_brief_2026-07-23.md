# WI / DC Pipeline Status
**July 23, 2026**

One campaign. One funnel. Remaining work: **`dc_assets_next` only**.

---

## Funnel

| Stage | Count | Meaning |
|---|---:|---|
| **1. Ran** | **267** | **197 WI** + **70 DC** |
| **2. Dupes** | **117** | Already in Salesforce |
| **3. Eligible to create** | **150** | **267 − 117** — true net-new create candidates |
| **4. Loaded to Salesforce** | **150** | Site__c created in this campaign (includes **1000 Independence Ave SW**) |

Eligible and loaded match: **150 / 150**.

**Remaining DC:** `data/dc_assets_next.csv` — **930** sites.

---

## Classified breakdown (130 sites with imagery + type on file)

| Imagery | Rooftop | Tower | Other | Unclear | Total |
|---|---:|---:|---:|---:|---:|
| NAIP | 15 | 13 | 0 | 0 | **28** |
| Nearmap vertical | 17 | 8 | 0 | 0 | **25** |
| Nearmap obliques | 59 | 8 | 8 | 1 | **76** |
| Zoom | 1 | 0 | 0 | 0 | **1** |
| **Total** | **92** | **29** | **8** | **1** | **130** |

---

## Nearmap image pulls (7/22 only)

| Nearmap view | Images |
|---|---:|
| Top-down (Vert) | **37** |
| Oblique (N/E/S/W) | **60** |
| **Total** | **97** |

Largest 7/22 batch (`211618`): **30** top-down + **44** oblique = **74** images (~46.3 MB on disk ≈ **47.03 MB** billed).

---

## What’s left

**Only** `data/dc_assets_next.csv` (**930** rows).

---

## Email blurb

Campaign ran **267** sites (**197 WI / 70 DC**). **117** Salesforce duplicates → **150** eligible → **150** loaded (including 1000 Independence Ave SW). Classification on file: **92 rooftop / 29 tower / 8 other / 1 unclear**. Nearmap on **7/22**: **97** images (**37** vert / **60** oblique); **47.03 MB** ≈ the **74**-image WI_next batch. Remaining: **930** DC next.
