-- Per-source observability: coverage gaps, nulls, and freshness. A VIEW (not a table) so
-- freshness_lag reflects wall-clock `now()` at query time rather than freezing at build time.
{{ config(materialized='view') }}

with latest_ingestion as (
    -- The latest ingestion per partition -- mirrors observations_current -- so a revision
    -- (a second content hash for a day already loaded) never double-counts expected_count.
    select distinct on (source, dataset, region, resolution, partition_date)
        source,
        dataset,
        region,
        resolution,
        expected_count,
        fetched_at
    from {{ source('raw', 'ingestion') }}
    order by source, dataset, region, resolution, partition_date, fetched_at desc
),

ingestion_agg as (
    select
        source,
        dataset,
        region,
        resolution,
        sum(expected_count) as expected_hours,
        max(fetched_at)     as last_fetched_at
    from latest_ingestion
    group by source, dataset, region, resolution
),

observation_agg as (
    select
        i.source,
        o.dataset,
        o.region,
        o.resolution,
        count(*) filter (where not o.is_missing) as present_hours,
        count(*) filter (where o.value is null)  as null_count,
        min(o.ts_utc)                            as min_ts_utc,
        max(o.ts_utc)                            as max_ts_utc
    from {{ source('raw', 'observations_current') }} o
    join {{ source('raw', 'ingestion') }} i on i.id = o.ingestion_id
    group by i.source, o.dataset, o.region, o.resolution
)

select
    ing.source,
    ing.dataset,
    ing.region,
    ing.resolution,
    ing.expected_hours,
    obs.present_hours,
    ing.expected_hours - obs.present_hours as gap_hours,
    obs.null_count,
    obs.min_ts_utc,
    obs.max_ts_utc,
    ing.last_fetched_at,
    now() - ing.last_fetched_at as freshness_lag
from ingestion_agg ing
join observation_agg obs
    using (source, dataset, region, resolution)
