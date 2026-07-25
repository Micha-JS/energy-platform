-- Weather forecast vintages, pivoted to one wide row per (issue_time, target_ts_utc). This is
-- the ONLY model permitted to read raw.forecast_observations (enforced by the no-lookahead guard
-- in tests/dbt/test_no_lookahead.py): it carries the issue_time / issue_date vintage dimension
-- forward as columns, so any downstream consumer inherits it and can never select a forecast
-- value without an issue-time predicate -- the no-lookahead invariant promised in M2.
select
    source,
    region,
    resolution,
    issue_date,
    issue_time,
    target_ts_utc,
    max(case when variable = 'shortwave_radiation' then value end) as ghi_w_m2,
    max(case when variable = 'direct_radiation'    then value end) as direct_radiation_w_m2,
    max(case when variable = 'diffuse_radiation'   then value end) as diffuse_radiation_w_m2,
    max(case when variable = 'temperature_2m'      then value end) as temperature_c,
    max(case when variable = 'cloud_cover'         then value end) as cloud_cover_pct,
    max(case when variable = 'wind_speed_10m'      then value end) as wind_speed_m_s,
    {{ berlin_calendar('target_ts_utc', prefix='target_') }}
from {{ source('raw', 'forecast_observations') }}
group by source, region, resolution, issue_date, issue_time, target_ts_utc
