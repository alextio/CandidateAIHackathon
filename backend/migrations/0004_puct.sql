-- PUCT Interchange docket-filing discovery layer (Source #3).
-- Source: PUCT Interchange Filing Search (interchange.puc.texas.gov), the
-- server-rendered docket route (?UtilityType=A&ControlNumber=<N>). Metadata-only
-- (v1) — the filing index, not the PDFs.
--
-- Run this in your Supabase project's SQL editor (any account).
-- Depends on 0001_ercot_projects.sql for set_updated_at() and
-- 0002_tceq.sql for the shared project_events + resolved_links tables, which
-- Source #3 REUSES (source='puct'); this migration creates only puct_filings.

-- ---------------------------------------------------------------------------
-- puct_filings: one row per (docket, filing item) per fetch snapshot.
-- ---------------------------------------------------------------------------
create table if not exists public.puct_filings (
    control_number     text not null,        -- docket/case id, e.g. 58481
    item_number        text not null,        -- filing sequence within the docket
    snapshot_date      date not null,        -- the run date this snapshot was pulled

    utility_type       text,                 -- Interchange UtilityType facet (e.g. 'A')
    filed_date         date,
    filing_party       text,                 -- who filed (company / agency / person)
    item_type          text,                 -- PUCT item-type code (COM, PRJ, ORD, APP, ...)
    item_type_label    text,                 -- human label for the code
    filing_description text,

    docket_title       text,                 -- case style, when Interchange exposes it
    source_url         text,                 -- per-item document listing on Interchange
    state              text not null default 'TX',  -- PUCT is Texas-only

    raw                jsonb not null default '{}'::jsonb,

    first_seen_at      timestamptz not null default now(),
    last_seen_at       timestamptz not null default now(),
    updated_at         timestamptz not null default now(),

    primary key (control_number, item_number, snapshot_date)
);

create index if not exists puct_filings_control_idx   on public.puct_filings (control_number);
create index if not exists puct_filings_snapshot_idx  on public.puct_filings (snapshot_date);
create index if not exists puct_filings_party_idx     on public.puct_filings (filing_party);
create index if not exists puct_filings_itemtype_idx  on public.puct_filings (item_type);
create index if not exists puct_filings_filed_idx     on public.puct_filings (filed_date);
create index if not exists puct_filings_raw_gin       on public.puct_filings using gin (raw);

drop trigger if exists trg_puct_filings_updated_at on public.puct_filings;
create trigger trg_puct_filings_updated_at
    before update on public.puct_filings
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- No new event/link tables: project_events and resolved_links (from 0002) are
-- source-agnostic and reused with source='puct'. The PUCT->shared mapping is:
--   project_events : permit_no  <- control_number
--                    event_type <- docket_opened | application_filed |
--                                   order_issued | agreement_approved
--                    entity     <- filing party / docket title  (county is null)
--   resolved_links : rn_number  <- normalized filing-party name (slug)
--                    permit_no   <- control_number
--                    inr         <- matched ercot_projects.inr (null if TCEQ/none)
--                    tceq_name   <- the PUCT party name (left side)
--                    ercot_name  <- matched ERCOT/TCEQ name
--                    raw.matched_source / raw.tceq_rn carry the TCEQ match
-- ---------------------------------------------------------------------------
