-- mart_coverage_monthly's five count columns must agree with each other. Returns a row (failure)
-- for any month whose flag, ratio or gap contradicts the hours it is supposed to summarise.
--
-- Same guard as assert_partial_months_are_flagged applies to the economics marts, on the mart that
-- introduces the monthly coverage grain rather than consumes it. It is checked separately rather
-- than added to that test's model list because the shape differs -- coverage is per ingested
-- series, not per priced scenario -- and forcing one query to cover both would mean padding
-- columns that mean nothing on one side.
--
-- present_hours > expected_hours is checked too, and it is not a tautology: present_hours counts
-- observation rows, expected_hours is derived from the Berlin calendar, so a duplicated ingestion
-- or a DST mishandling would show up here as a month reporting more hours than it has.
--
-- Empty-input sentinel, for the reason stated in every sibling test.

with rows_under_test as (
    select
        local_month,
        source,
        dataset,
        region,
        expected_hours,
        covered_hours,
        present_hours,
        gap_hours,
        completeness_ratio,
        is_partial_month
    from {{ ref('mart_coverage_monthly') }}
),

nothing_checked as (
    select
        null::date as local_month,
        '<mart_coverage_monthly is empty>' as source,
        null::text as dataset,
        null::text as region,
        0 as expected_hours,
        0 as covered_hours,
        0::bigint as present_hours,
        0 as gap_hours,
        null::numeric as completeness_ratio,
        null::boolean as is_partial_month
    where not exists (select 1 from rows_under_test)
)

select
    local_month, source, dataset, region,
    expected_hours, covered_hours, present_hours, gap_hours,
    completeness_ratio, is_partial_month,
    'coverage counts contradict each other: is_partial_month must equal present_hours < '
        || 'expected_hours, gap_hours must equal expected_hours - present_hours, and '
        || 'completeness_ratio must equal present_hours / expected_hours' as hint
from rows_under_test
where is_partial_month is null
   or is_partial_month <> (present_hours < expected_hours)
   or gap_hours <> expected_hours - present_hours
   or abs(completeness_ratio - present_hours::numeric / expected_hours) > 1e-6

union all

select
    local_month, source, dataset, region,
    expected_hours, covered_hours, present_hours, gap_hours,
    completeness_ratio, is_partial_month,
    'a month cannot hold more observed hours than the Berlin calendar gives it, nor declare more '
        || 'covered hours than it has' as hint
from rows_under_test
where present_hours > expected_hours
   or covered_hours > expected_hours

union all

select
    local_month, source, dataset, region,
    expected_hours, covered_hours, present_hours, gap_hours,
    completeness_ratio, is_partial_month,
    'mart_coverage_monthly is empty -- monthly coverage cannot be verified' as hint
from nothing_checked
