-- M8's optimality theorem, checked in the warehouse. Returns a row (failure) for any simulated
-- window and tariff where the hindsight optimum costs more than a scenario that is feasible for the
-- optimum's own problem.
--
-- TWO comparisons are checked here, and a third is deliberately NOT. That omission is the point of
-- this file, so it is stated before the arithmetic.
--
--   hindsight <= naive_continuous   CHECKED. M6's theorem, over the simulated span: the naive policy
--                                   satisfies every constraint the optimiser solves under and starts
--                                   from the same state of charge, so it is a feasible point.
--   hindsight <= forecast_driven    CHECKED. The *executed* trajectory obeys the SoC band, both
--                                   power ratings, both exclusivities and the AC-node identity, it
--                                   idles in exactly the hours the optimiser is constrained to idle
--                                   in, and it starts from the same state of charge -- so it too is
--                                   a feasible point of the hindsight problem. Clipping a plan to
--                                   feasibility cannot leave the feasible set; that is what it is
--                                   for. See src/energy_platform/dispatch/execution.py.
--   forecast_driven <= naive        NOT CHECKED, AND MUST NOT BE.
--
-- Nothing in the formulation makes forecast-driven dispatch beat naive self-consumption. Naive is
-- reactive and needs no forecast at all; a day-ahead plan commits in advance and is wrong whenever
-- the forecast is. Planning to hold charge for an evening peak on the strength of sunshine that does
-- not arrive costs money naive would never have spent, and on a window where the models are poor the
-- honest result is that forecasting lost. Asserting the ordering would make this suite fail exactly
-- when the platform produced its most interesting and most publishable output, and would create
-- pressure to quietly improve the seeded data until the assertion passed. It is reported --
-- mart_dispatch_regret.captured_value_share goes negative and says so -- and never asserted.
--
-- Comparison is on the objective (`*_cost_eur` here are all objective_eur), never on net cost, which
-- excludes the terminal valuation the optimiser was actually minimising.
--
-- The tolerance is the same 1e-5 as assert_optimal_never_costs_more_than_naive and for the same
-- derived reason: settlement rounds the energy cost, the feed-in revenue and the terminal credit
-- independently at a millionth of a euro, so each scenario carries up to +-1.5e-6 and a comparison
-- of two carries +-3e-6, plus snapped flows and the terminal SoC quantum. The two tests assert the
-- same theorem and must not disagree about what a rounding step costs.
{% set tolerance_eur = 1e-5 %}
{% set feasible_scenarios = ['naive_continuous', 'forecast_driven'] %}

with simulated as (
    select
        window_start,
        window_end,
        region,
        tariff_id,
        hindsight_cost_eur,
        naive_cost_eur,
        forecast_driven_cost_eur
    from {{ ref('mart_dispatch_regret') }}
    where is_simulated
),

-- A mart holding no simulated window has no optimum to contradict, so it would pass by silence.
-- A simulation that ran nothing looks exactly like a simulation that was never wrong.
nothing_checked as (
    select
        null::date as window_start,
        null::date as window_end,
        null::text as region,
        null::text as tariff_id,
        null::text as scenario,
        null::float8 as hindsight_cost_eur,
        null::float8 as scenario_cost_eur,
        'mart_dispatch_regret holds no simulated windows -- run `just forward-dispatch` before this '
            || 'test, or `just warehouse` for the whole sequence' as hint
    where not exists (select 1 from simulated)
)

{%- for scenario in feasible_scenarios %}
select
    window_start,
    window_end,
    region,
    tariff_id,
    '{{ scenario }}' as scenario,
    hindsight_cost_eur,
    {{ 'naive_cost_eur' if scenario == 'naive_continuous' else 'forecast_driven_cost_eur' }}
        as scenario_cost_eur,
    'the hindsight optimum costs more than {{ scenario }}, which is a feasible point of its own '
        || 'problem -- either that trajectory left the feasible set or the solver did not find the '
        || 'minimum' as hint
from simulated
where {{ 'naive_cost_eur' if scenario == 'naive_continuous' else 'forecast_driven_cost_eur' }}
        is null
   or hindsight_cost_eur
        > {{ 'naive_cost_eur' if scenario == 'naive_continuous' else 'forecast_driven_cost_eur' }}
          + {{ tolerance_eur }}

union all
{% endfor %}
select
    window_start, window_end, region, tariff_id, scenario,
    hindsight_cost_eur, scenario_cost_eur, hint
from nothing_checked
