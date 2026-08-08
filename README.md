# Texas Grid — Power Project Discovery & Stage Intelligence

**Find every Texas power/data-center project, see how far along it really is, and know whether it's actually moving — on one live map.**

A multi-source pipeline that fuses three public Texas datasets into a single, de-duplicated, geolocated view of the grid's development pipeline, then layers a calibrated ML model on top to score each project's lifecycle stage and momentum.

---

## 🎤 The 60-second pitch

> Texas is where the grid is being built — data centers, gas, batteries, solar — but the signal is scattered across three government systems that don't talk to each other. ERCOT tells you a project **exists** in the interconnection queue. TCEQ tells you it filed an **air permit**. PUCT tells you it hit **regulatory** review. None of them tell you **how far along a project is, or whether it's still moving or quietly stalled.**
>
> We built a pipeline that pulls all three live public sources, resolves them into one project per pin on a map of Texas, and runs an ML classifier that answers two questions no single source can: **what stage is this project at** (with a calibrated confidence and an honest "between X and Y" range), and **is it accelerating or stalling** (from months of queue history). The result is a live map an originator, investor, or developer can use to find real, moving deals — not just queue noise.

**One-liner:** *Bloomberg terminal for the Texas power buildout — three government feeds, one map, ML-scored maturity and momentum.*

---

## 🧩 What problem it solves

The Texas interconnection queue has **~2,000 projects** in the raw feed (~1,117 after we filter to data-center-relevant tech, growing to 1,238 across the multi-month ML training set), but most never get built. Anyone sourcing deals (developers, investors, EPCs, hardware/energy vendors) faces two hard questions:

1. **Which projects are real and advancing** vs. sitting in the queue as speculative placeholders?
2. **How far along** is each one, so you can time outreach to the right stage?

The answer requires stitching together three siloed government systems and reading between the lines. We automate exactly that.

---

## 🗺️ What it does (the demo)

A dark, live map of Texas (`texas.html`, MapLibre) with one pin per project, colored by the furthest stage it has reached:

| Color | Stage | Meaning |
|---|---|---|
| ⚪ Slate | `queued` | In the ERCOT queue only |
| 🟠 Amber | `permitting` | Has a linked TCEQ air permit (environmental review) |
| 🔵 Blue | `regulatory` | Has PUCT docket activity (public-utility review) |
| 🟢 Green | `approved` | ERCOT-approved to energize/synchronize |

Click any pin → a **project dossier**: a merged multi-source timeline, linked permits, a milestone checklist, capacity/fuel/technology, days in queue, and match confidence. The map is fed by a single `GET /map` endpoint that returns ready-to-render GeoJSON with precomputed insights.

**Live demo:** deployed to GitHub Pages via `.github/workflows/pages.yml` (publishes `frontend/public/texas.html`).

---

## 🔌 The three data sources

All are **Texas-only, public, no-scraping** feeds. Each keeps a full `raw` JSONB catch-all and stores history as dated snapshots.

### Source #1 — ERCOT (the queue) → *"it exists"*
Pulls the official ERCOT **GIS Report** (Generator Interconnection Status) from ERCOT's public MIS — no auth. Parses 4 sheets (Large/Small Gen, Inactive, Cancellation) into normalized `projects`, filtered to data-center-relevant tech (gas, battery, etc.). Stored as **monthly snapshots** keyed on `(inr, report_date)` so we can track history. ~1,117 matching projects; ~12.7k rows over 12 months.

### Source #2 — TCEQ (air permits) → *"it's getting real"*
**The thesis:** an air permit moving through TCEQ is a strong signal a queue project is heading to construction. Pulls TCEQ **Central Registry** air New Source Review (AIRNSR) records from `data.texas.gov` (Socrata API), turns them into dated project events, and **fuzzily links them back to ERCOT projects** by name + county (no lat/long exists in the feed). Token-set Jaccard match with county as a strong prior; each link is `resolved` / `review` / `unresolved`.

### Source #3 — PUCT (regulatory dockets) → *"it's in front of regulators"*
Pulls filing metadata from the **PUCT Interchange** docket route, normalizes filings into milestone events, and resolves parties back to ERCOT/TCEQ entities. Reuses the shared `project_events` + `resolved_links` tables (`source='puct'`).

---

## 📍 How geolocation works (no source has coordinates!)

None of the three feeds carry lat/long. Two-tier fallback whose **precision tracks project maturity**:

- **Tier 1 — county centroid**: all 254 TX counties (U.S. Census gazetteer). Every project has a county, so every project always gets a pin (`precision: "county"`).
- **Tier 2 — exact site**: EPA's **Facility Registry Service** (FRS), matched on the TCEQ RN number → real coordinates (`precision: "exact"`). ~50% of TCEQ RNs hit.

---

## 🧠 The ML layer — stage classifier + momentum

The queue tells you a project exists; it doesn't tell you *how far along* or *whether it's still moving*. This layer answers both, per project, **with honest uncertainty attached.** It reads only what discovery already persisted — no fetch step.

**The clever part (no labels problem):** there's no labeled dataset for "what stage is this project at." So a **deterministic rule table** reads ERCOT milestone columns and writes weak labels → training data without hand-labeling 1,238 rows. A **multinomial logistic regression** (boring on purpose — weak labels + explainable `coefficient × feature`) learns from them.

**8 lifecycle stages** (rank 0→7): `concept → fel1 → fel2_prefeed → feed → interconnection_agreement → fid → construction → cod`.

What the model adds over the raw rules:
- **Calibrated probabilities** (temperature scaling) — 0.7 means ~7-in-10 right, not a vibe.
- **A stage *range*, not just a guess** — split conformal prediction returns "between FEED and FID" that covers the truth ≥90% of the time.
- **Momentum** — `cod_slip_rate` (Theil-Sen slope of projected COD vs. report date): ~0 = on schedule, ~1 = slipping a month per month, <0 = pulling in. A project "18 months from COD" for three straight years is **stalled**, not early-stage — only the time series shows that.
- **Coverage** — every project gets an answer, even ones no rule fires on.

**Intellectual honesty built in** (great for judges): the model reports `rule_agreement` (≈ accuracy) *because the rules made the labels* — so accuracy is not independent proof. `construction` has no data source and is **never** predicted; missing stages are surfaced explicitly rather than hidden.

**Reference run** (6 ERCOT monthly reports — 6,815 rows, 1,238 projects — + 2,000 TCEQ permits): Macro F1 0.83, QWK 0.98, ECE 0.012, conformal coverage 0.974. These are from one run and reproduce by re-running `/classify`; only `macro_f1` is currently emitted by the pipeline.

---

## 🏗️ Architecture

```
                        ┌─────────────────────────────────────────┐
 ERCOT GIS Report ──►   │  FastAPI backend (Python)                │
 (public MIS, xlsx)     │                                          │
                        │  /discover/ercot ─┐                      │
 TCEQ Central Registry ─►  /discover/tceq ──┼─► Supabase (Postgres)│
 (Socrata SODA API)     │  /discover/puct ──┘   snapshots + raw    │
                        │                          │               │
 PUCT Interchange   ──► │  /classify (ML) ◄────────┘               │
 (docket filings)       │    rules → weak labels → logreg →        │
                        │    calibrated stage + conformal range    │
                        │    + Theil-Sen momentum                  │
                        │                                          │
 EPA FRS (coords)   ──► │  /map ──► GeoJSON (one pin per project)  │
                        └──────────────────────┬───────────────────┘
                                               │
                                       texas.html (MapLibre map, GitHub Pages)
```

- **Backend:** FastAPI + `uv`, deployed via `Dockerfile` / `render.yaml`. Interactive docs at `/docs`.
- **Storage:** Supabase (Postgres). Migrations `0001`–`0005` in `backend/migrations/`.
- **Frontend demo:** standalone `frontend/public/texas.html` (MapLibre GL, no build step, no map token).

---

## 🚀 Run it

```bash
# 1. Backend
cd backend
uv sync
cp .env.example .env          # add SUPABASE_URL + SUPABASE_SERVICE_KEY
# run migrations 0001–0005 in the Supabase SQL editor
uv run fastapi dev main.py    # http://localhost:8000  (docs at /docs)

# 2. Populate data (in order)
curl -X POST 'http://localhost:8000/discover/ercot?months=12'
curl -X POST 'http://localhost:8000/discover/tceq?geocode=true'
curl -X POST 'http://localhost:8000/discover/puct'
curl -X POST 'http://localhost:8000/classify'

# 3. Map data
curl 'http://localhost:8000/map?stage=permitting&on_thesis=true&min_mw=100'
```

Key endpoints: `/map` (GeoJSON pins) · `/projects` · `/events` · `/stages` · `/momentum` · `/projects/{inr}/stage` (full dossier for one project).

Tests: `cd backend && uv run pytest -q` (no DB or network needed — store layer runs against a fake client).

---

## 🎬 Suggested 1-minute demo flow

1. **Open the map** — "Every gas, battery, and data-center project in the ERCOT queue, live." Point at the hero count.
2. **Filter by color** — "Amber pins have air permits, blue have regulatory activity, green are approved to energize. That progression *is* the deal funnel."
3. **Click a green/blue pin** — show the dossier: merged timeline across ERCOT + TCEQ + PUCT, linked permits, milestone checklist.
4. **The ML punchline** — "This project is scored at stage FID with 0.8 confidence, and its completion date has held steady for 6 months — it's *moving*. This one next to it looks similar but has slipped a month every month for two years — it's stalled. Same queue, opposite investment thesis. That's what no single government feed can tell you, and what we surface automatically."

---

## 📚 Deeper docs

- `backend/README.md` — full source-by-source pipeline reference
- `docs/stage-classifier.md` — how the ML stage + momentum are produced, field-by-field
- `docs/frontend-map-integration.md` — wiring `/map` GeoJSON to any map library
