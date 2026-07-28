-- The forward simulation's output and the declared coverage windows must describe the same periods.
-- Returns a row (failure) for a simulated window the project no longer declares, or a declared
-- window nothing was written for.
--
-- The same two directions as assert_dispatch_windows_are_declared, and they fail for the same two
-- reasons: a stale window vanishes silently through mart_dispatch_regret's inner join, and a missing
-- one leaves the headline figure quietly absent rather than wrong -- which is worse, because nothing
-- looks broken.
--
-- WHAT "MISSING" MEANS HERE IS NARROWER THAN IT LOOKS, and that is the whole design. A window the
-- models could not be fitted for is NOT missing: `energy-platform forward-dispatch` writes it with
-- is_simulated = false and a not_simulated_reason. So this test does not require that every declared
-- window was simulated -- only that every declared window was *accounted for*. That distinction is
-- what stops the M7 failure mode from recurring: a window that simply vanished would let every
-- coverage test downstream pass over something nobody could see was absent, and "no model warmed up
-- yet" is a legitimate outcome that must still leave a trace.

-- Declared as a dependency rather than selected from: the "stale" half has to read the source
-- directly, because mart_dispatch_regret inner-joins the declaration and so cannot show a window
-- that is no longer declared. Without this edge the test would sit outside the mart's DAG and run on
-- a database the simulation has never written to.
-- depends_on: {{ ref('mart_dispatch_regret') }}

with declared as (
    {{ declared_coverage_windows() }}
),

recorded as (
    select distinct window_start, window_end
    from {{ source('derived', 'forward_dispatch_runs') }}
)

select
    r.window_start,
    r.window_end,
    'stale' as problem,
    'this window was simulated but dbt_project.yml no longer declares it; re-run '
        || '`just forward-dispatch` after changing coverage_windows so the derived tables match '
        || 'the declaration' as hint
from recorded r
left join declared d
    on d.window_start = r.window_start
    and d.window_end = r.window_end
where d.window_start is null

union all

select
    d.window_start,
    d.window_end,
    'missing' as problem,
    'this window is declared but the forward simulation wrote nothing for it -- not even a '
        || 'not-simulated marker; run `just forward-dispatch` (or `just warehouse` for the full '
        || 'sequence)' as hint
from declared d
left join recorded r
    on r.window_start = d.window_start
    and r.window_end = d.window_end
where r.window_start is null
