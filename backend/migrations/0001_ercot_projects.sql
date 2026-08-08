-- ERCOT project discovery layer: interconnection projects from the GIS Report.
-- Run this in your Supabase project's SQL editor (any account).

create table if not exists public.ercot_projects (
    -- One row per project per monthly report (snapshot history).
    inr                          text not null,
    report_date                  date not null,  -- month of the source report
    project_name                 text,
    size_category                text,           -- Large | Small
    status                       text not null default 'active', -- active | inactive | cancelled

    interconnecting_entity       text,
    poi_location                 text,
    county                       text,
    state                        text not null default 'TX', -- ERCOT is Texas-only
    cdr_reporting_zone           text,

    fuel                         text,           -- source code, e.g. SOL / WIN / GAS
    fuel_label                   text,           -- expanded label
    technology                   text,           -- source code, e.g. PV / WT / BA
    technology_label             text,
    capacity_mw                  double precision,

    gim_study_phase              text,
    projected_cod                date,
    ia_signed                    text,
    approved_for_energization    date,
    approved_for_synchronization date,

    inactive_date                timestamptz,
    cancel_date                  timestamptz,
    comment                      text,

    source_sheet                 text,
    source_file                  text,
    raw                          jsonb not null default '{}'::jsonb,

    first_seen_at                timestamptz not null default now(),
    last_seen_at                 timestamptz not null default now(),
    updated_at                   timestamptz not null default now(),

    primary key (inr, report_date)
);

create index if not exists ercot_projects_inr_idx      on public.ercot_projects (inr);
create index if not exists ercot_projects_report_idx   on public.ercot_projects (report_date);
create index if not exists ercot_projects_status_idx   on public.ercot_projects (status);
create index if not exists ercot_projects_fuel_idx     on public.ercot_projects (fuel);
create index if not exists ercot_projects_county_idx   on public.ercot_projects (county);
create index if not exists ercot_projects_state_idx     on public.ercot_projects (state);
create index if not exists ercot_projects_zone_idx     on public.ercot_projects (cdr_reporting_zone);
create index if not exists ercot_projects_capacity_idx on public.ercot_projects (capacity_mw);
create index if not exists ercot_projects_raw_gin      on public.ercot_projects using gin (raw);

-- Keep updated_at current on every upsert.
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_ercot_projects_updated_at on public.ercot_projects;
create trigger trg_ercot_projects_updated_at
    before update on public.ercot_projects
    for each row execute function public.set_updated_at();
