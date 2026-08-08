# The stage classifier

Where is each Texas power project in its life, how sure are we, and is it
actually moving? This document explains how the answer is produced and exactly
which fields a frontend can render.

Companion to [`frontend-map-integration.md`](frontend-map-integration.md), which
covers the `/map` pin layer. **The two use different vocabularies on purpose** —
see [The map's `stage` is not this `stage`](#the-maps-stage-is-not-this-stage).

---

## 1. The one-paragraph version

ERCOT publishes a monthly interconnection queue. TCEQ publishes air permits.
Neither says "this project is at FID." So a rule table reads the milestone
columns and writes down a stage for every project it can — that gives us
training data without anyone hand-labelling 1,238 rows. A logistic regression
then learns from those rules, and returns a **calibrated probability** for every
stage plus a **conformal range** of stages it will not rule out. Separately, six
months of snapshots per project are regressed to see whether its completion date
is holding still or receding — that is the momentum half.

Two numbers to internalise before reading further:

- **`confidence` is calibrated.** 0.7 means roughly seven in ten such calls are
  right. It is not a vibe score.
- **`accuracy` is not what you think.** The model is graded against the rules
  that taught it, so a high score largely means it learned the rules. See
  [§7](#7-what-this-model-does-not-tell-you).

---

## 2. The eight stages

Ordered earliest to latest. `rank` is the position, 0–7, and is what makes
"off by one stage" a smaller error than "off by four".

| rank | `stage` | Plain meaning | Where the evidence comes from |
|-----:|---------|---------------|-------------------------------|
| 0 | `concept` | In the queue, nothing else known | fallback when no rule fires |
| 1 | `fel1` | Screening study underway | `gim_study_phase` — "SS Started" |
| 2 | `fel2_prefeed` | Full interconnection study requested | `gim_study_phase` — "FIS Started" |
| 3 | `feed` | That study approved | `gim_study_phase` — "FIS Completed" |
| 4 | `interconnection_agreement` | IA signed | `ia_signed` column (a date) |
| 5 | `fid` | Money committed | linked TCEQ air permit + a signed IA |
| 6 | `construction` | Building | **no source exists** |
| 7 | `cod` | Energised / in service | `approved_for_energization` or `..._synchronization` |

**Three of these are never returned today** — `concept`, `fel1`, `construction`.
Not a bug, and not hidden: `GET /classify/labels` returns them in
`labels.missing_stages`, and the model run records them in
`metrics.missing_classes`. Reasons differ:

- `construction` — ERCOT publishes no construction milestone anywhere. Nothing
  to read.
- `fel1` — every live project past screening has also started its full study, so
  a later rule always wins. Reachable in principle, empty in practice.
- `concept` — every active project with a blank study phase turned out to have a
  signed IA, so no project reaches the fallback.

**Build the legend from `missing_stages`, not from this table.** The set changes
with the data.

---

## 3. How a stage is decided

### 3a. The rule table writes the training labels

Rules are checked **latest stage first**, and the first one that fires wins — a
project with both an IA and an energization date reads as `cod`.

```
cod                        energization or synchronization date present
construction               construction milestone present        (never fires)
fid                        ia_signed AND financial security / NTP
interconnection_agreement  ia_signed
feed                       full interconnection study approved
fel2_prefeed               full interconnection study requested
fel1                       screening study started or complete
concept                    (nothing fired)
```

Two supporting details worth knowing, because both were silently broken until
recently and both change what you see:

**`gim_study_phase` is a comma-separated triple, and its last segment is usually
a negation.** The live column holds values like `SS Completed, FIS Started, No
IA`. It is parsed segment by segment, and `No IA` is read as *no agreement*. A
scan that looked for "IA" anywhere in the string would flip 642 projects into
having an agreement they do not have.

**Withdrawn projects are excluded, not staged.** Cancelled or inactive projects
are dropped from training entirely: their last stage says where they stopped,
not where a live project of that shape sits. They still get a prediction, with
`withdrawn: true` on it. **Filter on `withdrawn` before showing a funnel.**

### 3b. The model learns from those labels

Multinomial logistic regression — deliberately boring, because the labels are
weak enough that a fancier model would only fit their noise more precisely, and
because `coefficient × feature` is an explanation a salesperson can read.

Labelled rows are split **50% train / 25% calibration / 25% test**. Training
never sees the calibration or test rows. Temperature scaling and the conformal
width are both learned on calibration; every reported metric comes from test.

The whole thing is fit and scored in one request. Nothing is pickled.
`model_version` is a content hash of the training inputs, so re-running on
unchanged data reproduces the same version instead of stacking near-identical
model rows.

### 3c. Where the rule overrides the model

If the rule fired on a **date ERCOT actually reported** — a signed IA, or an
energization/synchronization approval — the rule wins and `label_source` is
`"rule"`. A reported date is a fact, not something to take a model's opinion on.
Everything else is inferred from free text or from a linked permit, and there
the model's call stands (`label_source: "model"`).

In the reference run: 298 of 1,238 predictions were `"rule"`, 940 `"model"`.

---

## 4. What the model actually looks at

23 features. Useful to know when explaining a `contributions` entry to a user.

**From the latest snapshot** — `capacity_mw`, `log_capacity`, `phase_rank`
(how far the study phase text has got, 1–7), `cod_horizon_months` (months from
today to the projected COD), `has_ia_signed`, `has_energization_date`,
`has_synchronization_date`, `status`, `fuel`, `technology`, `size_category`.

**From the snapshot history** — `queue_age_months`, `n_snapshots`,
`cod_slip_rate`, `phase_velocity`, `capacity_cv`, `status_changes`,
`days_since_change`, `momentum_grade`.

**From linked TCEQ permits** — `n_permits`, `permit_score` (strength of the
name/county match), `has_active_permit`, `has_thesis_permit` (permit is
electric-power-generation NAICS).

Missing values are median-imputed **with an indicator column**, so the model can
learn that "no projected COD on file" is itself informative — which in this data
it is. Categories seen fewer than 25 times are folded into an `_other` bucket
rather than getting a coefficient fitted on four examples.

---

## 5. Momentum: is it moving?

Independent of the stage. Answers a different question, and origination usually
cares about it more: a project that has been "eighteen months from COD" for
three years is not early-stage, it is stalled.

`cod_slip_rate` is the slope of projected COD against report date, in **months
of slip per month elapsed**:

| value | reading |
|-------|---------|
| ~0 | holding schedule |
| ~1 | receding one month per month — treading water |
| >1 | losing ground |
| <0 | pulling in |

Fitted with Theil-Sen rather than least squares, because one developer pushing a
date out five years in a single filing is common here and would let that one
point set the whole slope.

`momentum_grade` buckets it: `accelerating` (< −0.25), `on_track` (< 0.25),
`slipping` (< 0.85), `stalled` (≥ 0.85), `unknown`.

**`unknown` and `null` are load-bearing.** Below three snapshots no slope is
estimated at all, and if the confidence interval spans more than 1.5 the grade
is refused. "Cannot tell yet" is a different claim from "not moving", and a UI
that renders them the same way is lying. Render `unknown` as its own state — not
as 0, not as `stalled`.

---

## 6. The API

All of these need Supabase configured and a discovery + classify run completed.
Without it every one returns **400** with
`"Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY."`

### Producing the data

```bash
curl -X POST "http://localhost:8000/discover/ercot?months=12"
curl -X POST "http://localhost:8000/discover/tceq"
curl -X POST "http://localhost:8000/classify"
```

`POST /classify` is pure computation over what discovery already stored — it
fetches nothing. Add `?persist=false` to see the summary without writing.

### `GET /classify/labels` — read this before trusting a run

Read-only, no training. Returns label coverage and, critically,
`labels.missing_stages`. A class listed there **cannot be predicted no matter
how good the metrics look**.

### `GET /stages` — the list view

Newest and most confident first.

| param | values |
|-------|--------|
| `stage` | any of the eight |
| `as_of` | `YYYY-MM-DD` |
| `model_version` | defaults to whatever is stored |
| `min_confidence` | `0.0`–`1.0` |
| `limit` / `offset` | ≤ 1000 |

```jsonc
{ "limit": 100, "offset": 0, "results": [ /* prediction rows, see below */ ] }
```

### `GET /momentum` — the movement view

Filters: `grade`, `as_of`, `limit`, `offset`. Returns `project_momentum` rows:
`inr`, `as_of`, `n_snapshots`, `first_report_date`, `last_report_date`,
`cod_slip_rate`, `cod_slip_lo`, `cod_slip_hi`, `cod_slip_mad`, `phase_velocity`,
`capacity_cv`, `status_changes`, `days_since_change`, `momentum_grade`.

### `GET /projects/{inr}/stage` — the detail view

Everything known about one project. **404** if that project has no prediction
yet. Real response, trimmed:

```jsonc
{
  "inr": "25INR0235",
  "entity_id": "ercot:25INR0235",
  "as_of": "2026-07-01",

  "stage": "fid",
  "label_source": "model",          // "model" | "rule"
  "rule_stage": "fid",
  "agrees_with_rule": true,

  "confidence": 1.0,                // calibrated P(top class)
  "margin": 1.0,                    // top minus runner-up
  "entropy": 0.0,                   // spread across all classes
  "expected_rank": 5.0,             // probability-weighted rank, 0–7

  "conformal_range": ["fid", "fid"],
  "conformal_alpha": 0.1,           // range covers truth >= 90% of the time

  "withdrawn": false,

  "probabilities": {                // object, not a string
    "fel2_prefeed": 0.0, "feed": 0.0,
    "interconnection_agreement": 0.0, "fid": 1.0, "cod": 0.0
  },
  "contributions": [                // array, not a string
    { "feature": "permit_score", "value": 3.82, "contribution": 3.36 }
  ],
  "justification": [                // array, not a string
    "ia_signed=2025-02-14 00:00:00",
    "financial_security_ntp=Y",
    "rule: IA signed with financial security / notice to proceed"
  ],

  "model_version": "…",
  "momentum": { /* the row from /momentum, or null */ }
}
```

`probabilities`, `contributions` and `justification` are **JSON objects and
arrays**. They were double-encoded as strings until recently; if you have
`JSON.parse` on them anywhere, remove it.

### Rendering rules that matter

**Never show `confidence` alone.** Pair it with `margin`. 0.5 confidence with
0.45 margin is a decisive call between two options; 0.5 with 0.02 is a coin
flip between adjacent stages. Same number, opposite meaning.

**`conformal_range` is the honest uncertainty.** Draw it as a band on the
lifecycle. `["fid", "fid"]` is a pinned call. `["fel2_prefeed", "feed"]` means
the model declines to choose. `conformal_alpha: 0.1` means the band contains the
true stage at least 90% of the time — **on average across all projects, not
within every stage**. A rare stage can be under-covered while the overall number
looks fine.

**`agrees_with_rule: false` is a feature, not an error.** It marks the projects
where the model saw something in the momentum or permit signals that the rule
table cannot see. 167 of 1,238 in the reference run. Worth surfacing.

**`expected_rank` is the right thing to sort a funnel by** — it is continuous
and uses the whole probability distribution, where `stage` throws away
everything but the argmax.

---

## 7. What this model does not tell you

Read this before putting a number on a slide.

**Accuracy is inflated by construction.** The rules produce the labels, and the
same columns the rules read are also features. The model is substantially
relearning the rules. `metrics.rule_agreement` is reported precisely so this
stays visible — and it equals `accuracy` exactly, because the test labels *are*
the rule labels. What the model genuinely adds over the rule table is calibrated
probabilities, conformal ranges, and the momentum and permit signals the rules
never see. It is not independent evidence that the rules are right.

**`fid` is not trustworthy yet.** Only 8 projects carry a FID label, per-class
F1 was 0.29. The stage exists and is populated; treat individual `fid` calls as
a lead to check, not a finding.

**Entity resolution is thin.** Linking a TCEQ permit to an ERCOT project is a
name-plus-county match. In the reference run 13 links resolved, 161 landed in
review, 1,804 were unresolved — most legitimately, since TCEQ air permits cover
all industry and only 25 of 2,000 were power generation. But it means
`has_active_permit` and `permit_score` are sparse, and `fid` depends on them.

**Labels can only be as current as the ERCOT report.** `as_of` defaults to the
newest monthly report, typically several weeks old.

---

## 8. Reference run

Numbers above come from one verified end-to-end run: 6 ERCOT monthly reports
(Feb–Jul 2026, 6,815 rows, 1,238 distinct projects) and 2,000 TCEQ permits
(1,072 geocoded). The TCEQ pull was row-capped for speed, so permit-derived
figures are a floor, not a ceiling.

**Labels** — 1,073 usable of 1,238 (86.7%); 165 dropped as withdrawn.
`fel2_prefeed` 692 · `interconnection_agreement` 217 · `cod` 81 · `feed` 75 ·
`fid` 8.

**Test split** (n = 269) — macro F1 0.828 · QWK 0.981 · rank MAE 0.037 ·
Brier 0.040 · log loss 0.191 · ECE 0.012 · accuracy 0.974 (= `rule_agreement`,
see §7).

**Per class F1** — `fel2_prefeed` 1.00 · `feed` 0.97 · `interconnection_agreement`
0.95 · `cod` 0.93 · `fid` 0.29.

**Calibration** — temperature 0.649, ECE 0.039 → 0.008.

**Conformal** — α = 0.10, target coverage 0.90, empirical 0.974, mean width 1.0
classes.

---

## The map's `stage` is not this `stage`

`GET /map` also returns a field called `stage`, with values `queued`,
`permitting`, `permit_only`. **That is a different thing.** It is a three-way
description of which sources know about a pin, computed directly in
`app/map_view.py`, and it never touches the model.

| | `/map` | `/stages`, `/projects/{inr}/stage` |
|---|---|---|
| values | `queued`, `permitting`, `permit_only` | the eight lifecycle stages |
| means | which sources have this project | how far along it is |
| produced by | `map_view.build_map` | the classifier |
| has confidence | no | yes |

Do not colour them from the same palette or a user will read one as the other.
To put lifecycle stage on the map today, join `/map` features to `/stages` on
the `inr` property — the map endpoint does not carry the model's output.

---

## Where the code lives

| file | what it holds |
|------|---------------|
| `backend/app/classify/stages.py` | the eight stages and the rule table |
| `backend/app/classify/phase.py` | parsing `gim_study_phase` — read this first |
| `backend/app/classify/labels.py` | rules → weak labels, and label diagnostics |
| `backend/app/classify/features.py` | the feature frame and preprocessing |
| `backend/app/classify/momentum.py` | Theil-Sen slip rate and grading |
| `backend/app/classify/train.py` | fit, calibrate, score, explain |
| `backend/app/classify/confidence.py` | temperature scaling and conformal ranges |
| `backend/app/classify/service.py` | orchestration behind the endpoints |
| `backend/app/classify/store.py` | Supabase reads and writes |
| `backend/migrations/0004_stage_classifier.sql` | `model_runs`, `stage_predictions`, `project_momentum` |
