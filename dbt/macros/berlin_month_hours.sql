{#
  The number of hours in a Europe/Berlin calendar month -- 743 in March, 745 in October, 720/744
  otherwise -- derived from the calendar, never counted from the rows.

  Same arithmetic as the per-day expectation in assert_no_missing_utc_hours.sql, and for the same
  reason: an expectation computed from the data under test shrinks in lockstep with the data, so
  a truncated or absent period passes by silence. Two Berlin-midnight instants subtracted in
  absolute time give the true elapsed hours, DST included.

  `month_col` must be the first day of the month as a date (i.e. date_trunc('month', ...)).
#}
{% macro berlin_month_hours(month_col) %}
    (extract(epoch from (
        (({{ month_col }} + interval '1 month')::date::timestamp at time zone 'Europe/Berlin')
        - ({{ month_col }}::timestamp at time zone 'Europe/Berlin')
    )) / 3600)::int
{% endmacro %}
