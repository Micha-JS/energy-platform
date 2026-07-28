-- The simulated span must sit inside the window it belongs to, and its hour expectation must be the
-- Berlin calendar's for that span. Returns a row (failure) otherwise.
--
-- Three ways this goes wrong, all of them silent without a check:
--
--   * The span escapes its window. mart_dispatch_regret reports costs over sim_start..sim_end while
--     joining on window_start..window_end, so a span reaching outside would attribute hours to a
--     window that does not contain them -- and, because windows must not overlap, would double-count
--     against a neighbour. The spring window abuts the March one exactly, so there is no slack.
--   * The span is inverted or empty. A simulation that covered nothing but claimed a span would
--     divide by a bogus expectation in completeness_ratio.
--   * expected_hours drifts from the calendar. It is computed by berlin_span_hours, the same macro
--     declared_coverage_windows uses and the same convention CoverageWindow.expected_hours applies
--     in Python -- so a 25-hour fall-back Sunday inside a span must be counted as 25. Recomputing it
--     here from the same macro would be circular, so this checks the invariant that actually
--     matters: the span's hours can never exceed the window's, and priced hours can never exceed
--     the span's.
--
-- Only simulated rows are checked. An unsimulated window has null span columns by construction, and
-- the not-null tests in _marts.yml cover the columns that must be present regardless.

with declared as (
    {{ declared_coverage_windows() }}
),

spans as (
    select
        m.window_start,
        m.window_end,
        m.region,
        m.tariff_id,
        m.sim_start,
        m.sim_end,
        m.simulated_days,
        m.expected_hours,
        m.priced_hours,
        d.expected_hours as window_expected_hours
    from {{ ref('mart_dispatch_regret') }} m
    join declared d
        on d.window_start = m.window_start
        and d.window_end = m.window_end
    where m.is_simulated
),

-- A mart with no simulated rows would pass every check below by having nothing to check.
nothing_checked as (
    select
        null::date as window_start,
        null::date as window_end,
        null::text as region,
        null::text as tariff_id,
        null::date as sim_start,
        null::date as sim_end,
        'mart_dispatch_regret holds no simulated windows -- run `just forward-dispatch` first, or '
            || '`just warehouse` for the whole sequence' as problem
    where not exists (select 1 from spans)
)

select window_start, window_end, region, tariff_id, sim_start, sim_end, problem
from (
    select
        window_start, window_end, region, tariff_id, sim_start, sim_end,
        case
            when sim_start < window_start or sim_end > window_end
                then 'the simulated span reaches outside its declared window'
            when sim_end < sim_start
                then 'the simulated span ends before it starts'
            when simulated_days < 1
                then 'a simulated window reports no simulated days'
            when expected_hours > window_expected_hours
                then 'the span claims more Berlin hours than the whole window has'
            when expected_hours < 1
                then 'the span claims no hours at all'
            when priced_hours > expected_hours
                then 'more hours were priced than the Berlin calendar says the span contains'
        end as problem
    from spans
) checked
where problem is not null

union all

select window_start, window_end, region, tariff_id, sim_start, sim_end, problem
from nothing_checked
