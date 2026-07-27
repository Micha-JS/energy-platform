-- Hindsight optimality, checked in the warehouse. Returns a row (failure) for any window and tariff
-- where the optimum's adjusted net cost exceeds a baseline that is feasible for its own problem.
--
-- This is a theorem, not an expectation. `naive_continuous` and `no_battery` each satisfy every
-- constraint the optimiser solves under -- AC-node balance, SoC continuity and bounds, both power
-- limits, both exclusivities -- and start from the same state of charge, so each is a feasible point
-- of the optimiser's problem, and a minimum cannot exceed the value at a feasible point. If this
-- ever fails, either a baseline drifted out of the feasible set or the solver did not find the
-- minimum; both are real defects, and neither is a tolerance to be widened.
--
-- `naive_telemetered` is deliberately NOT checked. The M3 generator restarts each Berlin day from a
-- fixed state of charge, so its trajectory violates SoC continuity at every midnight and is not a
-- dispatch any single battery could perform. Asserting the optimum beats it would be asserting
-- something about the generator's partition purity rather than about optimisation.
--
-- Comparison is on adjusted_net_cost_eur, the objective -- never on net_cost_eur, which excludes the
-- terminal valuation and which the optimiser was therefore never minimising.
--
-- The tolerance is a rounding budget, derived rather than guessed -- and it is not licence to widen
-- it further. adjusted_net_cost_eur is not one rounded number but three: settlement rounds the
-- energy cost, the feed-in revenue and the terminal credit independently at a millionth of a euro,
-- and the objective is their combination, so each scenario carries up to +-1.5e-6. Two scenarios are
-- compared, giving +-3e-6. On top of that sit the optimiser's snapped flows (_SOLVER_TOLERANCE_KWH,
-- measured well under 1e-6 EUR over a week) and the terminal SoC quantum (~2e-7 EUR, see
-- model._SOC_DELTA_DP). 1e-5 covers all of it with room to spare and is still four orders of
-- magnitude below any dispatch difference worth the name.
--
-- This matters more than the arithmetic suggests: under static_2024 the optimum and
-- naive_continuous settle to the *same* number, so the comparison sits exactly on the tolerance and
-- an asymmetric rounding step is all it would take to trip. The earlier 2e-6 budgeted one rounding
-- step per scenario where there are three.
{% set tolerance_eur = 1e-5 %}
{% set feasible_baselines = ['naive_continuous', 'no_battery'] %}

with optima as (
    select
        window_start,
        window_end,
        region,
        tariff_id,
        adjusted_net_cost_eur,
        {%- for baseline in feasible_baselines %}
        {{ baseline }}_cost_eur{{ "," if not loop.last }}
        {%- endfor %}
    from (
        select
            window_start,
            window_end,
            region,
            tariff_id,
            scenario,
            adjusted_net_cost_eur,
            {%- for baseline in feasible_baselines %}
            max(adjusted_net_cost_eur) filter (where scenario = '{{ baseline }}') over (
                partition by window_start, window_end, region, tariff_id
            ) as {{ baseline }}_cost_eur{{ "," if not loop.last }}
            {%- endfor %}
        from {{ ref('mart_dispatch_comparison') }}
    ) scenarios
    where scenario = 'optimal'
),

-- An empty mart has no optimum to contradict, so it would pass by silence without this. The
-- optimiser writing nothing looks exactly like an optimiser that was never wrong.
nothing_checked as (
    select
        null::date as window_start,
        null::date as window_end,
        null::text as region,
        null::text as tariff_id,
        null::text as baseline,
        null::float8 as optimal_cost_eur,
        null::float8 as baseline_cost_eur,
        'mart_dispatch_comparison holds no optimal rows -- run `just dispatch` before this test, '
            || 'or `just warehouse` for the whole sequence' as hint
    where not exists (select 1 from optima)
)

{%- for baseline in feasible_baselines %}
select
    window_start,
    window_end,
    region,
    tariff_id,
    '{{ baseline }}' as baseline,
    adjusted_net_cost_eur as optimal_cost_eur,
    {{ baseline }}_cost_eur as baseline_cost_eur,
    'the optimum costs more than {{ baseline }}, which is a feasible point of its own problem -- '
        || 'either that baseline left the feasible set or the solver did not find the minimum'
        as hint
from optima
where {{ baseline }}_cost_eur is null
   or adjusted_net_cost_eur > {{ baseline }}_cost_eur + {{ tolerance_eur }}

union all
{% endfor %}
select
    window_start, window_end, region, tariff_id, baseline,
    optimal_cost_eur, baseline_cost_eur, hint
from nothing_checked
