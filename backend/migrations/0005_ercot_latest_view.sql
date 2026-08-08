-- Fast map load: collapse ercot_projects snapshot history to one row per
-- project in Postgres, instead of shipping every snapshot to the app and
-- deduping there.
--
-- `ercot_projects` holds one row per project per monthly report (PK
-- inr, report_date). The map only needs each project's CURRENT state plus the
-- EARLIEST report_date we've seen it (its dwell time). This view computes both
-- server-side with a single DISTINCT ON scan, so /map reads ~one row per
-- project rather than projects × snapshots.
--
-- Run this in your Supabase project's SQL editor. Views in the public schema
-- are exposed through PostgREST automatically.

create or replace view public.ercot_projects_latest as
with firsts as (
    select inr, min(report_date) as first_report
    from public.ercot_projects
    group by inr
)
select distinct on (p.inr)
    p.*,
    f.first_report
from public.ercot_projects p
join firsts f using (inr)
order by p.inr, p.report_date desc;
