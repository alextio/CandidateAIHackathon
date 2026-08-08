-- Stage classifier (Source #3 output): predictions, momentum, model registry.
-- Run this in your Supabase project's SQL editor, after 0001 and 0002.
-- Depends on 0001 for the shared set_updated_at() trigger function.
--
-- Nothing here is a new source. These three tables hold what the classifier
-- derives from ercot_projects + tceq_permits + resolved_links, which is why
-- they carry an `as_of` rather than a `snapshot_date`: a row is an answer about
-- a date, not an observation made on one.

-- ---------------------------------------------------------------------------
-- model_runs: one row per trained model.
--
-- Predictions reference the model that produced them, so a retrain never
-- silently rewrites history and two model versions can be diffed over the same
-- projects. `model_version` is a content hash of the training inputs, so
-- re-running on unchanged data reproduces the row rather than adding one.
-- ---------------------------------------------------------------------------
create table if not exists public.model_runs (
    model_version text primary key,

    algo          text not null,          -- 'multinomial_logistic_regression'
    trained_at    timestamptz not null default now(),

    n_train       integer,
    n_features    integer,
    feature_names jsonb,                  -- ordered, matches the coefficient matrix
    classes       jsonb,                  -- ordered stage labels, lifecycle order

    -- Single scalar from temperature scaling. Stored because the calibrated
    -- probabilities cannot be reproduced without it.
    temperature   double precision,

    -- Held-out metrics: macro_f1, per_class_f1, rank_mae, qwk, ece, brier,
    -- log_loss, plus the conformal coverage and the rule-agreement number.
    metrics       jsonb,

    -- Coefficient matrix, so a prediction's explanation can be recomputed in
    -- SQL without re-running Python.
    coefficients  jsonb,
    intercepts    jsonb,

    git_sha       text,
    notes         text
);

comment on table public.model_runs is
    'One row per trained model. Predictions reference it so retrains never rewrite history.';
comment on column public.model_runs.metrics is
    'rule_agreement measures overlap with the rules that produced the labels, not correctness.';


-- ---------------------------------------------------------------------------
-- stage_predictions: where each project sits on the lifecycle, and how sure.
-- ---------------------------------------------------------------------------
create table if not exists public.stage_predictions (
    entity_id     text not null,          -- 'ercot:<inr>' for ERCOT-only projects
    as_of         date not null,
    model_version text not null references public.model_runs (model_version),

    stage         text not null,
    label_source  text not null default 'model',   -- 'rule' | 'model'

    -- What the deterministic rule table said, or null when no rule fired. Kept
    -- alongside the model's call so disagreement is queryable rather than
    -- invisible.
    rule_stage    text,

    probabilities jsonb not null,                  -- calibrated, one entry per class

    -- Three different questions, three different numbers:
    --   confidence  how sure of the top class          (calibrated max prob)
    --   margin      how much better than the runner-up (p1 - p2)
    --   entropy     how spread out overall             (normalized 0..1)
    -- Confidence 0.5 with margin 0.45 is decisive; confidence 0.5 with margin
    -- 0.02 is a coin flip between two stages. Only the first cannot tell them
    -- apart.
    confidence    double precision,
    margin        double precision,
    entropy       double precision,

    -- Probability-weighted position on the lifecycle. Moves smoothly where the
    -- argmax class jumps, which makes it the better thing to plot.
    expected_rank double precision,

    -- Ordinal conformal interval: the contiguous stage range covering the true
    -- stage with probability >= 1 - alpha. Contiguous by construction, so
    -- "between FEED and FID" is always a sentence that makes sense.
    conformal_lo    text,
    conformal_hi    text,
    conformal_alpha double precision,

    -- Terminal state, deliberately NOT a ninth stage: making it a class would
    -- put a non-ordinal value on an ordinal scale.
    withdrawn     boolean not null default false,

    -- Top coefficient x feature contributions toward the predicted class, and
    -- the rule evidence when a rule fired.
    contributions jsonb,
    justification jsonb,

    updated_at    timestamptz not null default now(),

    primary key (entity_id, as_of, model_version)
);

create index if not exists stage_predictions_stage_idx
    on public.stage_predictions (stage, as_of desc);
create index if not exists stage_predictions_asof_idx
    on public.stage_predictions (as_of desc);
create index if not exists stage_predictions_confidence_idx
    on public.stage_predictions (confidence desc);
create index if not exists stage_predictions_disagree_idx
    on public.stage_predictions (as_of desc)
    where rule_stage is not null and rule_stage is distinct from stage;

comment on column public.stage_predictions.expected_rank is
    'Sum over classes of probability x stage rank. Continuous, so it moves where argmax jumps.';
comment on column public.stage_predictions.withdrawn is
    'From cancel_date / inactive_date / status. A boolean, not a stage — withdrawal is not a lifecycle position.';

drop trigger if exists trg_stage_predictions_updated_at on public.stage_predictions;
create trigger trg_stage_predictions_updated_at
    before update on public.stage_predictions
    for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------------
-- project_momentum
--
-- Change-over-time metrics from the (inr, report_date) snapshot series. A
-- single snapshot says where a project claims to be; the series says whether it
-- is actually moving, which is the question origination cares about — a project
-- six months from COD for three running years is not early-stage, it is
-- stalled.
--
-- cod_slip_rate is the load-bearing number: Theil-Sen slope of projected_cod
-- against report_date, in months of COD movement per month of elapsed time.
--
--    ~0   COD is holding             on schedule
--    ~1   COD slips a month per month treading water
--    >1   COD receding faster than time passes
--    <0   COD pulling in             accelerating
--
-- Theil-Sen rather than least squares because a single wild COD revision is
-- common in this data and would dominate an OLS fit; Theil-Sen tolerates
-- corruption of up to ~29% of points in the simple-regression case.
--
-- Null, not zero, below 3 snapshots. "We cannot tell yet" and "not moving" are
-- different claims and must not render the same way.
-- ---------------------------------------------------------------------------
create table if not exists public.project_momentum (
    inr    text not null,
    as_of  date not null,

    n_snapshots       integer not null,
    first_report_date date,
    last_report_date  date,

    cod_slip_rate     double precision,
    cod_slip_lo       double precision,   -- Theil-Sen confidence interval
    cod_slip_hi       double precision,
    cod_slip_mad      double precision,   -- residual dispersion about the fit

    phase_velocity    double precision,   -- GIM phase ranks advanced per year
    capacity_cv       double precision,   -- coefficient of variation of capacity_mw
    status_changes    integer,
    days_since_change integer,

    -- Bucketed cod_slip_rate for display. 'unknown' below 3 snapshots, and also
    -- when the confidence interval is too wide to pick a bucket honestly.
    momentum_grade    text,

    updated_at timestamptz not null default now(),

    primary key (inr, as_of)
);

create index if not exists project_momentum_grade_idx
    on public.project_momentum (momentum_grade, as_of desc);
create index if not exists project_momentum_slip_idx
    on public.project_momentum (cod_slip_rate);

comment on column public.project_momentum.cod_slip_rate is
    'Theil-Sen slope of projected_cod vs report_date. 0 = holding schedule, 1 = slipping a month per month, <0 = pulling in.';
comment on column public.project_momentum.n_snapshots is
    'Snapshots backing the fit. Below 3 every slope column is null — unknown is not the same as zero.';

drop trigger if exists trg_project_momentum_updated_at on public.project_momentum;
create trigger trg_project_momentum_updated_at
    before update on public.project_momentum
    for each row execute function public.set_updated_at();
