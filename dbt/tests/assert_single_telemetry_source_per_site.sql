-- The energy spine and mart are one row per (ts_utc, region), so each site must resolve to exactly
-- one telemetry connector. Two connectors can legitimately write the same site -- the synthetic
-- demo generator and the real Fenecon reader both default to region = site_id -- and stg_telemetry
-- keeps them apart by carrying `source`. Without a choice the energy join would fan out, so this
-- test forces the choice explicitly rather than letting a silent blend or a doubled grain through.
--
-- Applies the same predicate the energy models use, so setting the var resolves the failure:
--   dbt build --vars '{telemetry_source: fenecon}'
select
    t.region,
    count(distinct t.source)                as source_count,
    string_agg(distinct t.source, ', ')      as sources,
    'set the telemetry_source var to one of these' as hint
from {{ ref('stg_telemetry') }} t
where {{ telemetry_source_predicate('t') }}
group by t.region
having count(distinct t.source) > 1
