# Texas Project Discovery Layer

Multi-source pipeline for Texas data-center / power development. Each source is
Texas-only (`state = 'TX'`), keeps a full `raw` JSONB catch-all, and stores
history as snapshots.

**Source #1 — ERCOT.** Pulls the interconnection queue from the official ERCOT
**GIS Report** (Generator Interconnection Status, product `PG7-200-ER`, report
type `15933`) and normalizes it into `projects`.

```
ERCOT public MIS  ->  GIS Report (.xlsx)  ->  parser  ->  projects  ->  Supabase
```

> The GIS/interconnection data is served from ERCOT's **public MIS** and needs
> no auth. ERCOT API credentials (`app/ercot/auth.py`, B2C ROPC OAuth) are wired
> in for *future* authenticated datasets (pricing, outages) but unused here.

## Layout

| File | Role |
|---|---|
| `app/ercot/gis_report.py` | Find + download the latest GIS `.xlsx` from MIS |
| `app/ercot/parser.py` | Parse all 4 project sheets → normalized `Project`s |
| `app/ercot/codes.py` | Fuel/technology code → label |
| `app/ercot/auth.py` | ERCOT OAuth token client (future authed datasets) |
| `app/models.py` | `Project` model (typed columns + full `raw` JSONB) |
| `app/db.py` | Supabase upsert (dedup on `inr`) |
| `app/discovery.py` | Orchestrates fetch → parse → persist |
| `main.py` | FastAPI endpoints |
| `migrations/0001_ercot_projects.sql` | Table schema |

Sheets ingested: **Large Gen** + **Small Gen** (`active`), **Inactive**
(`inactive`), **Cancellation Update** (`cancelled`).

Results are narrowed to data-center-relevant generation. Large/Small Gen are
filtered by **technology** (`CC, GT, IC, BA, EN, FC`); the Inactive/Cancellation
sheets have no technology column, so they're filtered by the equivalent
**fuels** (`Gas`, `Battery Storage`). Latest report ≈ 1,117 matching projects
(1,030 active + 80 inactive + 7 cancelled). Pass `all_technologies=true` /
`technologies=None` to keep everything (~2,012).

## Setup

```bash
cd backend
uv sync
cp .env.example .env   # fill in SUPABASE_URL + SUPABASE_SERVICE_KEY
```

Create the table in your Supabase project (any account): open the SQL editor and
run `migrations/0001_ercot_projects.sql`.

## Run

```bash
uv run fastapi dev main.py
```

- `POST /discover/ercot` — fetch the last **12** monthly reports (`?months=N`,
  1–23), parse, upsert to Supabase as monthly snapshots. Returns per-month
  counts. `?persist=false` to parse without writing; `?include_projects=true`
  to return the rows; `?all_technologies=true` to skip the data-center filter.
- `GET /projects` — query persisted projects. Filters: `status`, `fuel`,
  `county`, `zone`, `min_mw`, `report_date`, `limit`, `offset`.

```bash
curl -X POST 'http://localhost:8000/discover/ercot?months=12'
curl 'http://localhost:8000/projects?fuel=GAS&report_date=2026-07-01&min_mw=100'
```

### History model

Data is stored as **monthly snapshots**: one row per project per report month,
keyed on `(inr, report_date)`. This preserves queue history — you can track when
a project entered, and capacity/status changes month to month. 12 months of the
data-center-filtered set is ≈ 12.7k rows.

Interactive docs at `/docs`.

---

# Source #2 — TCEQ (air permits → "getting real" events)

**The thesis:** an air permit moving through TCEQ is a strong signal a queue
project is heading toward construction. This source turns TCEQ air **New Source
Review** (NSR) permit records into dated **project events** and fuzzily links
them back to ERCOT queue projects.

```
TCEQ Central Registry (AIRNSR)  ->  parser  ->  permit records
                                            ->  project events ("getting real")
                                            ->  entity resolution  ->  ercot_projects
```

### The source

TCEQ's **Central Registry Files** on the Texas Open Data Portal
(`data.texas.gov`, Socrata) — five regional datasets that together cover all of
Texas, public (no auth), queried via Socrata's SODA API filtered to
`program_code = AIRNSR`. Verified live fields per row: `ref_num_txt` (RN
number), `reg_ent_name` (site), `princ_name` / `princ_legal_name` (company /
CN), `re_phys_loc_addr_county`, `additional_id_text` (NSR permit number),
`indus_type_cd_name` (NAICS), status fields, and dates
(`affil_begin_dt` / `status_dt` / `affil_end_dt`).

> **Caveats.** No lat/long (resolution is name + county only). Affiliation-level,
> not transaction-level — we cannot cleanly split "application filed" from
> "issued", so events are `registered` / `status_change` / `affiliation_ended`.
> TCEQ uses sentinel dates (`1800-01-01`, `3000-12-31`) which are cleaned to null.

### Layout

| File | Role |
|---|---|
| `app/tceq/central_registry.py` | Fetch AIRNSR rows from the 5 regional Socrata datasets (paginated) |
| `app/tceq/parser.py` | Raw rows → `PermitRecord`s; derive `ProjectEvent`s |
| `app/tceq/codes.py` | NAICS power-gen allowlist, sentinel dates, status mapping |
| `app/tceq/resolve.py` | Fuzzy link TCEQ entities → `ercot_projects` (name + county) |
| `app/tceq/db.py` | Supabase upserts for the 3 tables |
| `app/tceq/discovery.py` | Orchestrates fetch → parse → events → resolve → persist |
| `migrations/0002_tceq.sql` | `tceq_permits`, `project_events`, `resolved_links` |

### Thesis filter

AIRNSR is dominated by upstream oil & gas (compressor stations), so every row
carries an `on_thesis` flag set when its NAICS is in the electric-power-generation
family (`2211xx`). Rows are kept regardless (pass `on_thesis_only=true` to
restrict), and the **entity-resolution join** to `ercot_projects` is the second
lever — an off-thesis-NAICS permit that matches a queue project is still surfaced.

### Entity resolution

No coordinates, so we match normalized **name + county**: token-set Jaccard on
company/site names (corporate suffixes stripped), with county agreement as a
strong prior and county disagreement capping the score. Each TCEQ entity yields a
`resolved_links` row — `resolved` (≥ 0.72), `review` (≥ 0.45), or `unresolved`.
Nothing is hard-deleted, so misses stay reviewable.

### Run

Create the tables: run `migrations/0002_tceq.sql` in the Supabase SQL editor
(needs `set_updated_at()` from `0001`).

- `POST /discover/tceq` — fetch AIRNSR permits (all regions, or `?regions=`),
  derive events, resolve to ERCOT, upsert as a snapshot keyed on the run date.
  `?persist=false` to skip writes; `?resolve=false` to skip the ERCOT join;
  `?on_thesis_only=true` to keep only power-gen NAICS; `?max_rows_per_region=N`
  for quick tests; `?include_records=true` to return the rows.
- `GET /events` — query derived events. Filters: `entity`, `county`,
  `event_type`, `source`, `since`, `until`, `limit`, `offset`.

```bash
curl -X POST 'http://localhost:8000/discover/tceq?max_rows_per_region=500'
curl 'http://localhost:8000/events?event_type=registered&county=HARRIS&since=2024-01-01'
```

### History model

Each `/discover/tceq` run is one **snapshot** keyed on the run date:
`tceq_permits` PK `(rn_number, permit_no, snapshot_date)`. `project_events` is
source-agnostic (PK `(source, permit_no, event_type, event_date)`) so future
sources can emit into the same "getting real" stream.

---

# Map layer (Texas project pins)

`GET /map` returns a **GeoJSON FeatureCollection** — one de-duplicated pin per
project — for a Leaflet/Mapbox layer. No source feed carries lat/long, so pins
use a two-tier geolocation whose precision tracks project maturity:

- **Tier 1 — county centroid** (`app/geo/county_centroids.py`, all 254 TX
  counties from the U.S. Census gazetteer). Every project has a county, so every
  project always gets a pin. `precision: "county"`.
- **Tier 2 — exact site** via **EPA's FRS** FeatureServer
  (`app/geo/frs.py`), matched on `PGM_SYS_ACRNM='TX-TCEQ ACR'` + the RN number.
  Filled during `/discover/tceq` (`geocode=true`, ~50% of RNs hit).
  `precision: "exact"`.

A pin's **stage** encodes the funnel: `queued` (ERCOT only, county centroid) →
`permitting` (has a TCEQ air permit, resolved to a queue project, usually exact)
→ `permit_only` (permit with no ERCOT match). ERCOT projects already shown as a
`permitting` pin are not repeated as `queued`.

```bash
# apply migrations/0003_tceq_geocode.sql, then re-run discovery to fill coords
curl -X POST 'http://localhost:8000/discover/tceq?max_rows_per_region=500&geocode=true'

curl 'http://localhost:8000/map?resolved_only=true'            # only ERCOT-linked pins
curl 'http://localhost:8000/map?stage=permitting&on_thesis=true'
curl 'http://localhost:8000/map?source=ercot&min_mw=100&county=Hood'
```

Each feature's `properties` carry `name`, `county`, `stage`, `precision`,
`capacity_mw`/`fuel`/`technology` (when ERCOT-linked), `rn_number`/`permit_no`,
`inr`, and `resolution_status`/`resolution_score`. The response `meta` block
totals pins by precision and by stage.

---

# Derived layer — stage classifier

**The question:** the queue tells you a project exists. It does not tell you how
far along it is, or whether it is still moving. This layer answers both, per
project, with an honest uncertainty attached.

```
ercot_projects (history)  ->  momentum      ->
tceq_permits + resolved_links -> permits    ->  features -> model -> stage_predictions
rule table                ->  weak labels   ->
```

No fetch step of its own — it reads what Sources #1 and #2 already persisted.

### Where the labels come from

There is no labelled training set for "what stage is this project in", so the
labels are made by a deterministic rule table (`app/classify/stages.py`) reading
milestone columns. That has a hard limit worth stating plainly:

| Stage | Evidence | Available? |
|---|---|---|
| `concept` | in the queue, nothing else | yes |
| `fel1` | screening study | yes, via `gim_study_phase` |
| `fel2_prefeed` | full interconnection study | yes, via `gim_study_phase` |
| `feed` | study approved | yes, via `gim_study_phase` |
| `interconnection_agreement` | `ia_signed` | yes |
| `fid` | financial security / notice to proceed | **only via a linked TCEQ permit** |
| `construction` | construction milestone | **no source — never predicted** |
| `cod` | energization / synchronization | yes |

`construction` has no source at all, so it gets zero labels and can never be
predicted. `GET /classify/labels` reports that under `missing_stages` rather
than letting an absent class look like a rare one.

The model is multinomial logistic regression — boring on purpose. The labels are
weak, so their noise floor sits well above the gap between a linear model and a
boosted one, and `coefficient x feature` is an explanation a non-technical reader
can act on. Its agreement with the rules is reported as `rule_agreement` and is
*not* accuracy: the rules made the labels, so agreement mostly measures overlap.

### What it adds over the rules

- **Calibrated probabilities.** Temperature scaling on a held-out split, so 0.7
  means roughly seven in ten right. `ece` before and after is in `metrics`.
- **A stage range, not just a guess.** Split conformal prediction returns a
  contiguous interval — "between FEED and FID" — that covers the true stage at
  least `1 - alpha` of the time.
- **Momentum.** `cod_slip_rate` is the Theil-Sen slope of projected COD against
  report date: ~0 holding schedule, ~1 slipping a month per month, negative
  pulling in. Null below three snapshots, because "cannot tell yet" is not "not
  moving". A project six months from COD for three running years is not an
  early-stage opportunity, it is a stalled one, and only the series shows that.
- **Coverage.** Every project gets an answer, including the ones no rule fires
  on.

### Setup

Run `migrations/0004_stage_classifier.sql` in the Supabase SQL editor (needs
`set_updated_at()` from `0001`). It creates `model_runs`, `stage_predictions`
and `project_momentum`. No new credentials — it uses the same `SUPABASE_URL` +
`SUPABASE_SERVICE_KEY` as everything else.

### Run

- `POST /classify` — label, fit, calibrate, score every project, persist.
  `?persist=false` to see the summary without writing; `?as_of=YYYY-MM-DD` to
  reconstruct what was knowable then; `?alpha=` for the conformal miscoverage
  rate. Training and scoring happen in one pass, so no model file is written —
  `model_version` is a content hash of the training inputs, so re-running on
  unchanged data reproduces the same version instead of stacking model rows.
- `GET /classify/labels` — weak-label coverage, read-only. Worth reading first.
- `POST /classify/momentum` — recompute the series metrics on their own.
- `GET /stages` — stored predictions. Filters: `stage`, `as_of`,
  `model_version`, `min_confidence`, `limit`, `offset`.
- `GET /momentum` — stored momentum. Filters: `grade`, `as_of`.
- `GET /projects/{inr}/stage` — everything known about one project, including
  whether the model and the rule disagree.

```bash
curl -X POST 'http://localhost:8000/classify?persist=false'   # dry run
curl -X POST 'http://localhost:8000/classify'
curl 'http://localhost:8000/stages?stage=interconnection_agreement&min_confidence=0.7'
curl 'http://localhost:8000/momentum?grade=stalled'
curl 'http://localhost:8000/projects/26INR0001/stage'
```

### Reading a prediction

`confidence`, `margin` and `entropy` answer three different questions. A call at
confidence 0.5 with margin 0.45 is decisive; 0.5 with margin 0.02 is a coin flip
between two adjacent stages, and only `margin` tells them apart. `expected_rank`
is the probability-weighted lifecycle position — it moves smoothly where the
argmax stage jumps, so it is the better thing to plot. `withdrawn` is a boolean,
never a stage: withdrawal is not a position on the lifecycle.

### Tests

```bash
cd backend && uv run pytest -q
```

No database or network needed — the store layer is tested against a fake client.
