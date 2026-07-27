{#
  The declared `coverage_windows`, one row per window, with the DST-correct number of Europe/Berlin
  hours each one claims. Emits a complete select, to be used as a CTE body:

      declared as ({{ declared_coverage_windows() }})

  Columns: (window_start, window_end, expected_hours).

  Sibling of declared_coverage_hours, which answers the same question at monthly grain for the
  economics marts. Both derive day lengths by subtracting Berlin midnight instants in absolute
  time, so a window containing the spring-forward Sunday claims 167 hours and one containing
  fall-back claims 169 -- never a naive 7 x 24. That is the same convention
  energy_platform.dispatch.windows.CoverageWindow.expected_hours uses on the Python side, which is
  what lets the optimiser's hour count and this one agree by construction rather than by luck.

  The expectation must come from the calendar and never from the rows: an expectation counted from
  the data under test shrinks in lockstep with a truncated window and passes by silence.
#}
{% macro declared_coverage_windows() %}
    {%- set coverage = var('coverage_windows') -%}
    select
        start_date as window_start,
        end_date   as window_end,
        (extract(epoch from (
            ((end_date + 1)::timestamp at time zone 'Europe/Berlin')
            - (start_date::timestamp at time zone 'Europe/Berlin')
        )) / 3600)::int as expected_hours
    from (values
        {%- for w in coverage %}
        (date '{{ w.start }}', date '{{ w.end }}'){{ "," if not loop.last }}
        {%- endfor %}
    ) as windows (start_date, end_date)
{% endmacro %}
