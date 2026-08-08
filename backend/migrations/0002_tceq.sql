-- TCEQ air-permit discovery layer (Source #2).
-- Source: TCEQ Central Registry Files on data.texas.gov (Socrata),
-- filtered to program_code = AIRNSR (air New Source Review permits).
-- Run this in your Supabase project's SQL editor (any account).
--
-- Depends on 0001_ercot_projects.sql for the shared set_updated_at() trigger fn.

-- ---------------------------------------------------------------------------
-- tceq_permits: one row per (regulated entity, permit) per fetch snapshot.
-- ---------------------------------------------------------------------------
create table if not exists public.tceq_permits (
    rn_number              text not null,        -- Regulated Entity number, e.g. RN104089412
    permit_no              text not null default '',  -- NSR permit number (may be blank)
    snapshot_date          date not null,        -- the run date this snapshot was pulled

    program_code           text,                 -- e.g. AIRNSR
    entity_name            text,                 -- site / facility name (reg_ent_name)
    company_name           text,                 -- affiliated customer (princ_name / CN)
    company_legal_name     text,

    county                 text,
    city                   text,
    address                text,
    state                  text not null default 'TX',  -- TCEQ is Texas-only

    naics_code             text,
    naics_label            text,
    on_thesis              boolean not null default false,  -- electric-power-gen NAICS

    entity_status          text not null default 'unknown', -- active | inactive | unknown
    company_status         text,

    affiliation_begin_date date,
    affiliation_end_date   date,
    status_date            date,

    region                 text,                 -- which regional dataset
    source_dataset         text,                 -- Socrata four-by-four id
    raw                    jsonb not null default '{}'::jsonb,

    first_seen_at          timestamptz not null default now(),
    last_seen_at           timestamptz not null default now(),
    updated_at             timestamptz not null default now(),

    primary key (rn_number, permit_no, snapshot_date)
);

create index if not exists tceq_permits_rn_idx        on public.tceq_permits (rn_number);
create index if not exists tceq_permits_permit_idx    on public.tceq_permits (permit_no);
create index if not exists tceq_permits_snapshot_idx  on public.tceq_permits (snapshot_date);
create index if not exists tceq_permits_county_idx    on public.tceq_permits (county);
create index if not exists tceq_permits_naics_idx     on public.tceq_permits (naics_code);
create index if not exists tceq_permits_thesis_idx    on public.tceq_permits (on_thesis);
create index if not exists tceq_permits_company_idx   on public.tceq_permits (company_name);
create index if not exists tceq_permits_raw_gin       on public.tceq_permits using gin (raw);

drop trigger if exists trg_tceq_permits_updated_at on public.tceq_permits;
create trigger trg_tceq_permits_updated_at
    before update on public.tceq_permits
    for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------------
-- project_events: dated "project is getting real" signals, source-agnostic.
-- ---------------------------------------------------------------------------
create table if not exists public.project_events (
    source        text not null default 'tceq',  -- which pipeline source produced it
    permit_no     text not null default '',
    event_type    text not null,                 -- registered | status_change | affiliation_ended
    event_date    date not null,

    rn_number     text,
    entity        text,                          -- best available site/company name
    county        text,
    state         text not null default 'TX',
    raw           jsonb not null default '{}'::jsonb,

    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    primary key (source, permit_no, event_type, event_date)
);

create index if not exists project_events_type_idx   on public.project_events (event_type);
create index if not exists project_events_date_idx    on public.project_events (event_date);
create index if not exists project_events_county_idx  on public.project_events (county);
create index if not exists project_events_entity_idx  on public.project_events (entity);
create index if not exists project_events_rn_idx      on public.project_events (rn_number);
create index if not exists project_events_raw_gin     on public.project_events using gin (raw);

drop trigger if exists trg_project_events_updated_at on public.project_events;
create trigger trg_project_events_updated_at
    before update on public.project_events
    for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------------
-- resolved_links: fuzzy links from TCEQ entities to ercot_projects.
-- Non-matches are kept with status='unresolved' (never hard-deleted) so a
-- human can review them later.
-- ---------------------------------------------------------------------------
create table if not exists public.resolved_links (
    source        text not null default 'tceq',
    rn_number     text not null,               -- TCEQ regulated entity
    permit_no     text not null default '',
    inr           text,                        -- matched ercot_projects.inr (null if unresolved)

    status        text not null default 'unresolved', -- resolved | review | unresolved
    score         double precision not null default 0, -- 0..1 match confidence
    method        text,                        -- how the match was made

    tceq_name     text,                        -- normalized name used on the TCEQ side
    ercot_name    text,                        -- normalized name matched on the ERCOT side
    county        text,

    raw           jsonb not null default '{}'::jsonb,

    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    primary key (source, rn_number, permit_no)
);

create index if not exists resolved_links_inr_idx    on public.resolved_links (inr);
create index if not exists resolved_links_status_idx on public.resolved_links (status);
create index if not exists resolved_links_score_idx  on public.resolved_links (score);
create index if not exists resolved_links_county_idx on public.resolved_links (county);

drop trigger if exists trg_resolved_links_updated_at on public.resolved_links;
create trigger trg_resolved_links_updated_at
    before update on public.resolved_links
    for each row execute function public.set_updated_at();
