-- The M10 load split is an identity, not a definition:
--     household_load == load_base + ac_power
-- Returns a row (failure) for any hour where it does not hold.
--
-- WHY THIS TEST CAN EXIST AT ALL. The three columns are produced independently -- the generator
-- emits all three, and a real house emits whichever ones it can actually meter. Had household_load
-- instead been *computed* downstream as load_base + ac_power, this file could not be written: the
-- identity would be true by construction and would assert nothing about the producer. Keeping the
-- total a first-class channel is what converts a naming convention into a checkable claim, and it
-- is the whole reason M10 did not deprecate household_load.
--
-- THE TOLERANCE IS 1e-9 AND THAT IS DELIBERATELY TIGHT. The synthetic generator quantises the two
-- components at the emission boundary and then publishes the quantised sum, so the identity holds
-- to floating-point dust rather than to within three independent roundings. Rounding all three
-- separately would let them disagree by a whole 1 Wh (5e-4 kWh) -- five hundred thousand times this
-- tolerance -- and a budget wide enough to absorb that would also absorb a real defect. If this
-- test starts failing by ~1e-3, the generator has stopped forming the total from the emitted
-- components; the fix is there, not here.
--
-- ROWS WITH A MISSING COMPONENT ARE EXCLUDED, NOT FAILED. That is not leniency, it is the real
-- house: the Fenecon meters consumption, not appliances, so unless a separate AC meter is mapped,
-- load_base and ac_power simply never arrive and household_load stands alone. Requiring all three
-- would fail a correctly-configured deployment for reporting exactly what it can measure. The
-- empty-relation sentinel below is what stops that exclusion from turning the test into a vacuous
-- pass on a warehouse where nothing separable was ever ingested.
with checked as (
    select
        ts_utc,
        region,
        household_load_kwh - load_base_kwh - ac_power_kwh as residual_kwh
    from {{ ref('mart_hourly_energy') }}
    where household_load_kwh is not null
      and load_base_kwh is not null
      and ac_power_kwh is not null
),

-- No checkable hours at all means either an empty mart or a warehouse in which no producer ever
-- separated the two components. On the seeded demo data the synthetic generator always does, so
-- this firing means the split has silently stopped being ingested -- exactly the regression that
-- would otherwise pass by silence.
nothing_checked as (
    select
        null::timestamptz as ts_utc,
        '<no hours carry a separable load split>' as region,
        0::numeric as residual_kwh
    where not exists (select 1 from checked)
)

select ts_utc, region, residual_kwh
from checked
where abs(residual_kwh) > 1e-9
union all
select ts_utc, region, residual_kwh from nothing_checked
