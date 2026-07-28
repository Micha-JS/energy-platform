-- data_mode must be present exactly on telemetry rows, and absent everywhere else. Returns a row
-- (failure) for any mart_data_quality row that breaks either half of that.
--
-- The point is the FIRST half. M9's dashboard banner states whether the warehouse was built from
-- the synthetic demo generator or from a real house, and it reads that from data_mode rather than
-- deciding it in Python. A third telemetry connector added without a branch in the
-- telemetry_data_mode macro would write telemetry rows with a NULL mode, and the banner would
-- report "unknown" about data it is looking straight at. This fails the build instead.
--
-- The second half is not symmetry for its own sake: mapping 'smard' or 'open_meteo' to one of the
-- two modes would claim a price series is synthetic or real household data, which is a category
-- error. NULL is the correct answer there and is asserted as such.
--
-- Empty-input sentinel, as every singular test here carries: a guard that passes because the mart
-- is empty is indistinguishable from one that passes because the mart is right.

with rows_under_test as (
    select
        source,
        dataset,
        region,
        resolution,
        data_mode,
        dataset in ({{ telemetry_datasets() }}) as is_telemetry
    from {{ ref('mart_data_quality') }}
),

nothing_checked as (
    select
        '<mart_data_quality is empty>' as source,
        null::text as dataset,
        null::text as region,
        null::text as resolution,
        null::text as data_mode,
        null::boolean as is_telemetry
    where not exists (select 1 from rows_under_test)
)

select
    source, dataset, region, resolution, data_mode, is_telemetry,
    'a telemetry dataset must carry a data_mode -- add a branch to the telemetry_data_mode macro '
        || 'for this connector' as hint
from rows_under_test
where is_telemetry and data_mode is null

union all

select
    source, dataset, region, resolution, data_mode, is_telemetry,
    'a non-telemetry dataset must not claim a data_mode -- prices and weather are neither '
        || 'synthetic nor real household telemetry' as hint
from rows_under_test
where not is_telemetry and data_mode is not null

union all

select
    source, dataset, region, resolution, data_mode, is_telemetry,
    'mart_data_quality is empty -- the data mode cannot be verified' as hint
from nothing_checked
