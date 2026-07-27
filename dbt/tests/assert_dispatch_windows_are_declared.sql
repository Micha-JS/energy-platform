-- The optimiser's output and the declared coverage windows must describe the same periods.
-- Returns a row (failure) for a solved window the project no longer declares, or a declared window
-- nothing was solved for.
--
-- Both directions matter, and they fail for different reasons:
--
--   * A solved window that is no longer declared is STALE. `coverage_windows` moved and the derived
--     tables were not re-solved; mart_dispatch_comparison inner-joins the declaration so those rows
--     vanish from the mart silently. This makes the disappearance loud.
--   * A declared window with no solve is MISSING. `just dbt-build` ran without `just dispatch`, and
--     the headline savings figure would be quietly absent rather than wrong -- which is worse,
--     because nothing looks broken.
--
-- Sites are not enumerated here: which sites exist is data, not declaration. The completeness of a
-- window's scenarios is covered by the mart's uniqueness test and by
-- assert_optimal_never_costs_more_than_naive, which fails when a baseline is absent.

-- Declared as a dependency rather than selected from: the "stale" half of this test has to read the
-- source directly, because mart_dispatch_comparison inner-joins the declaration and so cannot show
-- a window that is no longer declared. Without this edge the test would sit outside the mart's DAG
-- and run on a database where the optimiser has never written, before `just dispatch` can exist.
-- depends_on: {{ ref('mart_dispatch_comparison') }}

with declared as (
    {{ declared_coverage_windows() }}
),

solved as (
    select distinct window_start, window_end
    from {{ source('derived', 'dispatch_runs') }}
)

select
    s.window_start,
    s.window_end,
    'stale' as problem,
    'this window was solved but dbt_project.yml no longer declares it; re-run `just dispatch` '
        || 'after changing coverage_windows so the derived tables match the declaration' as hint
from solved s
left join declared d
    on d.window_start = s.window_start
    and d.window_end = s.window_end
where d.window_start is null

union all

select
    d.window_start,
    d.window_end,
    'missing' as problem,
    'this window is declared but nothing has been solved for it; run `just dispatch` (or '
        || '`just warehouse` for the full build -> solve -> build sequence)' as hint
from declared d
left join solved s
    on s.window_start = d.window_start
    and s.window_end = d.window_end
where s.window_start is null
