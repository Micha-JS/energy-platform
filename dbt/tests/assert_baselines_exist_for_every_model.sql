-- Every model must be reported beside the naive baselines it is meant to be judged against.
--
-- "Baselines first, and always reported" is the credibility test M7 rests on, and a convention
-- nobody enforces is a convention that quietly lapses -- most likely at exactly the moment someone
-- is selecting rows to make a chart. So it is structural: a (site, target, window) that carries any
-- model row and is missing either baseline fails the build.
--
-- The converse is deliberately NOT asserted. A window can legitimately hold baselines and no model
-- (too few days to train on -- the two DST weeks are exactly this), and failing that case would
-- force a model to be fabricated where none could honestly be fitted.
{% set required_baselines = ['persistence', 'seasonal_naive'] %}

with scope as (
    select distinct site, target, window_start, window_end
    from {{ ref('mart_forecast_eval') }}
    where role = 'model'
),

present as (
    select distinct site, target, window_start, window_end, model_key
    from {{ ref('mart_forecast_eval') }}
    where role = 'baseline'
),

required as (
    select scope.*, baseline
    from scope
    cross join (values {% for name in required_baselines %}('{{ name }}'){{ "," if not loop.last }}{% endfor %}) as b (baseline)
)

select
    required.site,
    required.target,
    required.window_start,
    required.window_end,
    required.baseline as missing_baseline,
    'a model is reported for this window with no ' || required.baseline || ' row to read it '
        || 'against -- rerun `energy-platform forecast` for the whole window' as hint
from required
left join present
    on  present.site = required.site
    and present.target = required.target
    and present.window_start = required.window_start
    and present.window_end = required.window_end
    and present.model_key = required.baseline
where present.model_key is null
