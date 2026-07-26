-- No month may be presented as complete unless it is. Returns a row (failure) whenever a monthly
-- economics row has fewer priced hours than the calendar month holds but is_partial_month is
-- false, or the completeness ratio disagrees with the counts it is supposed to summarise.
--
-- This is the guard on the M4 coverage convention as it extends to aggregates: the seeded windows
-- are a week long, so every month in the demo is partial, and a consumer reading total_cost_eur
-- without the flag would read a week's electricity as a month's bill.
--
-- Both economics marts are checked, because the convention is shared and a copy-paste divergence
-- between them is exactly the drift worth catching. Failing rows carry a `hint`.
{% set models = ['mart_tariff_counterfactuals', 'mart_solar_economics'] %}

with rows_under_test as (
    {%- for model in models %}
    select
        '{{ model }}' as model_name,
        local_month,
        region,
        scenario,
        tariff_id,
        priced_hours,
        expected_hours,
        completeness_ratio,
        is_partial_month
    from {{ ref(model) }}
    {%- if not loop.last %}
    union all
    {%- endif %}
    {%- endfor %}
),

-- An empty mart has no rows to contradict, so it would pass by silence without this.
nothing_checked as (
    select
        '<economics marts are empty>' as model_name,
        null::date as local_month,
        null::text as region,
        null::text as scenario,
        null::text as tariff_id,
        0::bigint as priced_hours,
        0 as expected_hours,
        null::numeric as completeness_ratio,
        null::boolean as is_partial_month
    where not exists (select 1 from rows_under_test)
)

select
    model_name, local_month, region, scenario, tariff_id,
    priced_hours, expected_hours, completeness_ratio, is_partial_month,
    'a month with priced_hours < expected_hours must be flagged partial, and completeness_ratio '
        || 'must equal priced_hours / expected_hours' as hint
from rows_under_test
where is_partial_month <> (priced_hours < expected_hours)
   or is_partial_month is null
   or abs(completeness_ratio - priced_hours::numeric / expected_hours) > 1e-9

union all

select
    model_name, local_month, region, scenario, tariff_id,
    priced_hours, expected_hours, completeness_ratio, is_partial_month,
    'both economics marts are empty -- coverage flags cannot be verified' as hint
from nothing_checked
