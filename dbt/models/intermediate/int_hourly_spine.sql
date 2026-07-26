-- The canonical UTC-hour grid, generated from the DECLARED coverage windows (the
-- `coverage_windows` var) -- never inferred from min/max of the data, so the two disjoint seeded
-- windows do not manifest a seven-month void between them. Bounds are Berlin-midnight instants,
-- and generate_series steps in absolute UTC time, so DST is correct by construction: 23 rows
-- across the spring-forward day, 25 across the fall-back day, every UTC hour exactly once.
--
-- The time grid is var-driven, but the SITE dimension comes from data: the grid is cross-joined
-- with the distinct sites present in staged telemetry, so any `--site` value flows through to the
-- energy mart (the spine is no longer pinned to the default site). Grain: (ts_utc, region). If
-- telemetry is entirely absent the spine is empty -- acceptable, as the seed always has >=1 site.
{% set windows = var('coverage_windows') %}

with windows (start_date, end_date) as (
    values
    {%- for w in windows %}
        (date '{{ w.start }}', date '{{ w.end }}'){{ "," if not loop.last }}
    {%- endfor %}
),

bounds as (
    select
        -- Berlin midnight of the first day .. Berlin midnight after the last day (exclusive).
        (start_date::timestamp at time zone 'Europe/Berlin')          as lo,
        ((end_date + 1)::timestamp at time zone 'Europe/Berlin')      as hi
    from windows
),

hours as (
    select generate_series(lo, hi - interval '1 hour', interval '1 hour')::timestamptz as ts_utc
    from bounds
)

select
    ts_utc,
    region,
    {{ berlin_calendar('ts_utc') }}
from hours
cross join (
    -- Filtered by the same telemetry_source predicate the energy join uses, so a site present
    -- only in the unselected source cannot manifest a full grid of all-NULL telemetry rows.
    select distinct t.region
    from {{ ref('stg_telemetry') }} t
    where {{ telemetry_source_predicate('t') }}
) sites
