{#
  Hours of each Europe/Berlin calendar month that the declared `coverage_windows` claim, so a
  monthly row can separate "the pipeline never covered this month" from "it covered it and the
  data gapped". Emits a complete select, to be used as a CTE body:

      declared as ({{ declared_coverage_hours() }})

  One row per month, (local_month, covered_hours). Day lengths come from Berlin midnight instants
  subtracted in absolute time, so a DST day contributes 23 or 25 hours -- the same convention as
  berlin_month_hours, which is what makes covered_hours and expected_hours comparable.

  Lives in a macro because both monthly marts carry the column and the two must not drift.
#}
{% macro declared_coverage_hours() %}
    {%- set coverage = var('coverage_windows') -%}
    select
        date_trunc('month', local_date)::date as local_month,
        sum(
            (extract(epoch from (
                ((local_date + 1)::timestamp at time zone 'Europe/Berlin')
                - (local_date::timestamp at time zone 'Europe/Berlin')
            )) / 3600)::int
        )::int as covered_hours
    from (
        select generate_series(start_date, end_date, interval '1 day')::date as local_date
        from (values
            {%- for w in coverage %}
            (date '{{ w.start }}', date '{{ w.end }}'){{ "," if not loop.last }}
            {%- endfor %}
        ) as windows (start_date, end_date)
    ) days
    group by 1
{% endmacro %}
