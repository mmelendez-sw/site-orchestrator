# Asset Classifier — Google Gemini vs Anthropic Claude
**For:** Leadership review  
**Subject:** AI provider comparison for aerial site classification (tower vs rooftop)  
**Date:** June 2026  

---

## Executive summary

We built a pipeline that takes a list of addresses/coordinates, pulls aerial imagery (free NAIP + optional Nearmap subscription), and uses a vision AI model to classify each location as **tower**, **rooftop**, **other**, or **unclear**, plus whether **cellular equipment is visible**.

We have piloted this on sample assets and are scaling to **674 Washington, DC sites** in batches.

**Both providers work with the same pipeline** — only the AI backend changes. The choice comes down to **speed, cost, reliability, and classification quality** for our workload.

| | **Google Gemini (Flash)** | **Anthropic Claude (Sonnet 4.6)** |
|---|---|---|
| **Speed (observed)** | ~33 sec/site | ~54 sec/site |
| **Cost (est.)** | Free tier for pilots; low $ at scale | ~$0.05/site (~$34 for 674 sites) |
| **Pilot experience** | Worked well; occasional 503 overload | Works; required model ID update (retired models) |
| **Best for** | High volume, tight budget, fast iteration | Production quality, stakeholder confidence |

**Recommendation:** Run **Gemini Flash for bulk screening** (674 DC sites) and use **Claude Sonnet for spot-checks, low-confidence rows, and stealth/hard cases** — or pick one provider based on whether speed/cost or accuracy confidence matters more for this phase.

---

## What the pipeline does (context for non-technical readers)

For each site, the system:

1. Geocodes the address (free US Census / OpenStreetMap)
2. Downloads a **wide public aerial photo** (NAIP, ~250 m, ~1 m resolution)
3. Optionally downloads **high-resolution Nearmap imagery** (top-down + four 45° angles) — critical for rooftop antennas
4. Sends all images to an **AI vision model** with a structured prompt
5. Outputs a CSV row: site type, confidence score, equipment yes/no, evidence text, and saved photos for human review

The AI step is the only part that differs between Gemini and Claude. Imagery fetch, geocoding, and reporting are identical.

---

## Speed comparison

| Component | Time | Notes |
|-----------|------|-------|
| Geocoding + imagery fetch | ~15–20 sec | Same for both providers |
| **AI classification** | **Gemini ~6 sec** / **Claude ~22 sec** | Main difference |
| Built-in rate-limit pause | 12 sec | Same for both (configurable) |
| **Total per site** | **~33 sec (Gemini)** / **~54 sec (Claude)** | From our observed runs |

**Runtime impact for 674 DC sites (single-threaded):**

| Provider | Est. total runtime |
|----------|-------------------|
| Gemini Flash | ~6 hours |
| Claude Sonnet | ~10 hours |

Sites that trigger extra AI passes (equipment recheck, zoom for unclear/stealth) add time on both platforms; Claude adds more per extra pass.

**Why Claude is slower:** We use Claude **Sonnet 4.6** (accuracy-focused). Gemini **2.5 Flash** is optimized for speed. Claude **Haiku 4.5** would narrow the gap but is still typically slower than Gemini Flash.

---

## Cost comparison

### Google Gemini

- **Free tier:** Generous for pilots (~10 requests/min, daily quotas). Our 7-asset pilot and 10-asset DC batch fit comfortably at **$0**.
- **At scale (674 sites):** Paid tier is inexpensive for Flash-class models; exact $ depends on billing setup. Multi-image vision calls are cheap relative to Claude.
- **Hidden cost:** Engineer time if free-tier quotas exhaust mid-run (wait for reset or enable billing).

### Anthropic Claude

- **No meaningful free tier** for this volume — billed from first API call.
- **Estimated ~$0.05/site** on Sonnet 4.6 (typical DC site with full Nearmap, 1–2 AI calls).
- **674 DC sites ≈ $30–50** total (Sonnet); **~$10–15** if using Haiku as primary.
- **Predictable:** Pay-as-you-go; no daily quota cliff.

---

## Quality and accuracy

| Factor | Gemini Flash | Claude Sonnet |
|--------|--------------|---------------|
| Multi-image fusion (NAIP + Nearmap obliques) | Good | Good to very good |
| Rooftop equipment in shadow | Can miss; recheck pass helps | Strong; tends to cite specific views |
| Structured JSON output | Native, reliable | Tool-based JSON, reliable |
| Confidence calibration | Sometimes over/under confident | Often more conservative (lower scores when uncertain) |
| Stealth / disguised towers | Adequate with zoom pass | Strong reasoning on ambiguous structures |

**From our pilot (7 sample assets + 10 DC sites on Gemini):** Both correctly identified obvious towers and urban rooftops when Nearmap obliques were available. Low-confidence calls (30–40% site confidence) occurred on both — these are flagged for human review via saved `chips/` photos regardless of provider.

**Practical guidance:** Neither replaces human review on low-confidence rows (< 60%). The AI is best used as **triage at scale**, not ground truth.

---

## Reliability and operations

### Google Gemini — pros

- **Fast** — best throughput for large batches
- **Free tier** — low friction for pilots and re-runs
- **Simple API key** from Google AI Studio
- **Model fallback chain** in code (hops between Flash variants when one quota is exhausted)

### Google Gemini — cons

- **503 overload errors** — we hit this on dc_007 during a batch run (“model experiencing high demand”). Retries usually work; occasionally requires re-run.
- **Quota limits** on free tier — long runs (674 sites) may hit daily caps; need billing for uninterrupted production.
- **Model churn** — Google renames/deprecates models periodically.

### Anthropic Claude — pros

- **Stable, production-grade API** — typical for enterprise workflows
- **Strong vision + reasoning** — better evidence text for stakeholder reports
- **Predictable billing** — no surprise quota walls mid-batch
- **Clear model documentation** — but models do retire (we had to update IDs when Sonnet 4.0 retired June 15, 2026)

### Anthropic Claude — cons

- **Slower** — ~60% longer per site in our tests
- **Costs money from day one** — ~$30–50 for full DC list on Sonnet
- **Model ID maintenance** — must update when Anthropic retires model snapshots (404 errors if IDs are stale)
- **529 overloaded** possible under heavy load (similar to Gemini 503, less frequent in our testing)

---

## Side-by-side recommendation matrix

| If your priority is… | Choose |
|----------------------|--------|
| Fastest completion of 674 DC sites | **Gemini Flash** |
| Lowest direct API cost | **Gemini Flash** (free/low tier) |
| Best classification quality / audit trail | **Claude Sonnet** |
| Predictable spend, no quota surprises | **Claude** (paid from start) |
| Hybrid / best of both | **Gemini for bulk** → **Claude re-run on low-confidence rows** |

---

## Suggested path forward

**Option A — Cost & speed first (recommended for DC bulk run)**  
- Run all 674 sites on **Gemini 2.5 Flash**  
- Human-review rows with `site_confidence < 0.6` or `error` populated  
- Est. ~6 hours runtime, minimal API cost  

**Option B — Quality first**  
- Run all 674 sites on **Claude Sonnet 4.6**  
- Est. ~10 hours runtime, ~$30–50 API cost  
- Stronger evidence text for executive reporting  

**Option C — Hybrid (best long-term)**  
- Gemini screens all 674 → ~$0–low cost, fast  
- Claude re-classifies only flagged rows (~10–20% = 70–135 sites) → ~$5–10 incremental  
- Combines speed of bulk processing with Claude quality where it matters  

---

## What we are not comparing here

These are **outside** the Gemini vs Claude decision:

- **Nearmap subscription** — imagery cost (~GB/month allowance); same regardless of AI provider. Already in use; ~15 MB for 7 pilot assets.
- **NAIP / geocoding** — free public data.
- **Human review labor** — required for low-confidence results either way.

---

## Appendix: confidence scores (for report readers)

Each result row includes separate scores:

- **`site_confidence`** (0–1) — how sure the AI is about **tower vs rooftop vs other**
- **`cell_equipment_confidence`** (0–1) — how sure the AI is about **equipment visible yes/no**

A rooftop call at **0.4 site confidence** means “probably rooftop, but review the photos.” It is **not** an overall pipeline score.

---

*Prepared from pilot runs (June 2026): 7-asset multi-state sample, 10-asset DC batch (Gemini), DC batch in progress (Claude). Figures are observational; exact token billing available in Google AI Studio and Anthropic Console.*
