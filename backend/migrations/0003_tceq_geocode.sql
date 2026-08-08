-- Map layer: exact coordinates for TCEQ permits (Tier-2 geocoding).
-- Coordinates come from EPA's FRS FeatureServer, matched on RN number
-- (PGM_SYS_ACRNM='TX-TCEQ ACR'). RNs missing from FRS keep null coords and the
-- API falls back to the county centroid at query time.
-- Run in the Supabase SQL editor after 0002_tceq.sql.

alter table public.tceq_permits
    add column if not exists latitude      double precision,
    add column if not exists longitude     double precision,
    add column if not exists geo_accuracy_m integer,
    add column if not exists geo_source    text,          -- e.g. 'epa_frs'
    add column if not exists geocoded_at   timestamptz;

create index if not exists tceq_permits_latlng_idx
    on public.tceq_permits (latitude, longitude);
