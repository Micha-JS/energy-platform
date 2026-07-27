-- The terminal adjustment must be reconstructible from the columns the mart exposes. Returns a row
-- (failure) for any row where the credit is not the product of its three published factors, or
-- where the adjusted cost is not the net cost less that credit.
--
-- This is the test behind mart_dispatch_comparison's central claim: "the adjustment is fully
-- exposed, so it can be undone or recomputed at any other valuation without re-solving". That claim
-- is what lets a reader probe terminal-value sensitivity without running the solver, and it is only
-- worth making if it is exact. It was not: the discharge-efficiency factor settlement applies lived
-- only in the derived zone's `battery` jsonb, which no model reads, so a reader following the
-- documented recipe overstated the credit by ~5%. Both identities are now checked here rather than
-- documented and hoped for.
--
-- The efficiency factor is not decoration. A kWh in the store can only ever reach the AC node
-- through the discharge leg, so terminal_value_ct_kwh is what a *delivered* kWh is worth and
-- sqrt(round_trip_efficiency) is what converts a stored one into it. Dropping it prices energy the
-- battery cannot actually deliver.
--
-- Tolerance is half a rounding step of the settlement boundary. The product itself is bit-exact --
-- the same three doubles multiplied in the same order on both sides -- but the persisted value was
-- rounded to a millionth of a euro by Python (banker's) and is compared here against Postgres
-- arithmetic, so the two can differ by half a step at a tie and by nothing otherwise. Anything
-- larger means settlement and this recipe have parted company, which is the defect this exists for.
{% set tolerance_eur = 5e-7 %}

-- An empty mart is caught by assert_optimal_never_costs_more_than_naive, which fails when there is
-- no optimum to contradict. Repeating that guard here would be a second copy of the same check.

with reconstructed as (
    select
        window_start,
        window_end,
        region,
        tariff_id,
        scenario,
        terminal_value_eur,
        adjusted_net_cost_eur,
        net_cost_eur,
        terminal_value_ct_kwh / 100 * terminal_discharge_efficiency * terminal_soc_delta_kwh
            as recomputed_terminal_eur
    from {{ ref('mart_dispatch_comparison') }}
)

select
    window_start,
    window_end,
    region,
    tariff_id,
    scenario,
    'terminal_value_eur' as identity,
    terminal_value_eur as persisted,
    recomputed_terminal_eur as reconstructed,
    'the credit is not the product of terminal_value_ct_kwh / 100, '
        || 'terminal_discharge_efficiency and terminal_soc_delta_kwh -- settlement and the recipe '
        || 'this mart documents have diverged' as hint
from reconstructed
where abs(terminal_value_eur - recomputed_terminal_eur) > {{ tolerance_eur }}

union all

select
    window_start,
    window_end,
    region,
    tariff_id,
    scenario,
    'adjusted_net_cost_eur' as identity,
    adjusted_net_cost_eur as persisted,
    net_cost_eur - terminal_value_eur as reconstructed,
    'the adjusted cost is not net_cost_eur - terminal_value_eur, so the adjustment cannot be '
        || 'undone by adding the credit back' as hint
from reconstructed
where abs(adjusted_net_cost_eur - (net_cost_eur - terminal_value_eur)) > {{ tolerance_eur }}
