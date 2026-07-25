{#
  Derive Europe/Berlin local calendar columns from a UTC timestamptz. This is the single place
  DST semantics live: `at time zone 'Europe/Berlin'` returns the local wall-clock timestamp, so
  the 23-hour (spring-forward) and 25-hour (fall-back) days resolve correctly. Never key
  uniqueness on the local columns -- the October fall-back day has two distinct ts_utc at local
  02:00, so (local_date, local_hour) is deliberately non-unique.
#}
{% macro berlin_calendar(ts_col, prefix='') %}
    {{ ts_col }} at time zone 'Europe/Berlin'                          as {{ prefix }}local_ts,
    ({{ ts_col }} at time zone 'Europe/Berlin')::date                 as {{ prefix }}local_date,
    extract(hour from {{ ts_col }} at time zone 'Europe/Berlin')::int  as {{ prefix }}local_hour
{% endmacro %}
