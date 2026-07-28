{#
  The DST-correct number of Europe/Berlin hours in an inclusive span of calendar dates.

      {{ berlin_span_hours('sim_start', 'sim_end') }}

  Both arguments are SQL expressions evaluating to `date`, so this works on literals from a values
  list and on columns from a table alike -- which is the whole reason it exists as a macro rather
  than staying inlined in declared_coverage_windows. M8 needs the same arithmetic over a *simulated*
  span, which is a pair of columns rather than a pair of declared literals, and a second copy of the
  expression would be a second place for the DST handling to be got subtly wrong.

  Derived by subtracting two Berlin midnight instants in absolute time, so a span containing the
  spring-forward Sunday claims 167 hours where a naive 7 x 24 would claim 168, and one containing
  fall-back claims 169. Same convention as
  energy_platform.dispatch.windows.CoverageWindow.expected_hours on the Python side, which is what
  lets the two agree by construction rather than by luck.
#}
{% macro berlin_span_hours(start_expr, end_expr) %}
    (extract(epoch from (
        (({{ end_expr }} + 1)::timestamp at time zone 'Europe/Berlin')
        - ({{ start_expr }}::timestamp at time zone 'Europe/Berlin')
    )) / 3600)::int
{% endmacro %}
