-- The canonical UTC-hour grid, generated from the DECLARED coverage windows (the
-- `coverage_windows` var) -- never inferred from min/max of the data, so the two disjoint seeded
-- windows do not manifest a seven-month void between them. Bounds are Berlin-midnight instants,
-- and generate_series steps in absolute UTC time, so DST is correct by construction: 23 rows
-- across the spring-forward day, 25 across the fall-back day, every UTC hour exactly once.
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
    'home'::text as region,
    {{ berlin_calendar('ts_utc') }}
from hours
