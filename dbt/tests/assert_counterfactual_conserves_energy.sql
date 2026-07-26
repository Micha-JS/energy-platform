-- The no-battery counterfactual has no storage, so the AC-node identity is exact:
--     pv + grid_import == household_load + grid_export
-- Returns a row (failure) for any hour where it does not hold, or where an hour manages to import
-- and export at once. The tolerance is 1e-9 kWh -- a millionth of the 1 Wh emission quantum, so it
-- admits floating-point dust and nothing else.
--
-- The battery scenario is deliberately NOT checked here: its identity carries charge and discharge
-- terms and is already asserted upstream on mart_hourly_energy (balance_residual_kwh).
--
-- Hours with a gap in any term are excluded rather than failed -- a NULL residual is the correct
-- outcome of a missing input, and the null-propagation itself is what the
-- counterfactual_nulls_propagate_not_greatest_zero unit test guards. The empty-relation sentinel
-- below is what stops that exclusion from turning the whole test into a vacuous pass.
with checked as (
    select
        ts_utc,
        region,
        pv_production_kwh + grid_import_kwh
            - household_load_kwh - grid_export_kwh as residual_kwh,
        least(grid_import_kwh, grid_export_kwh)    as both_directions_kwh
    from {{ ref('int_hourly_counterfactual') }}
    where scenario = 'no_battery'
      and pv_production_kwh is not null
      and household_load_kwh is not null
      and grid_import_kwh is not null
      and grid_export_kwh is not null
),

-- No checkable hours at all means an empty or entirely-gapped model, which must fail rather than
-- pass by silence -- the same rule as the DST singular tests.
nothing_checked as (
    select
        null::timestamptz as ts_utc,
        '<no priced counterfactual hours>' as region,
        0::numeric as residual_kwh,
        0::numeric as both_directions_kwh
    where not exists (select 1 from checked)
)

select ts_utc, region, residual_kwh, both_directions_kwh
from checked
where abs(residual_kwh) > 1e-9 or both_directions_kwh > 1e-9
union all
select ts_utc, region, residual_kwh, both_directions_kwh from nothing_checked
