{#
  Which telemetry connector the energy models read.

  Two connectors can write the same site -- the synthetic demo generator and the real Fenecon
  reader both default to `region = site_id` -- and stg_telemetry keeps them apart by carrying
  `source` in its grain. The energy spine and mart are one row per (ts_utc, region), so exactly
  one of them must be selected; joining without a predicate would fan out every spine row.

  Default (var unset) is NO filter, deliberately: silently defaulting to 'synthetic' would drop a
  real house's data, and defaulting to 'fenecon' would empty the demo. With two sources present
  and no var set, assert_single_telemetry_source_per_site fails and names the var to set:

      dbt build --vars '{telemetry_source: fenecon}'
#}
{% macro telemetry_source_predicate(alias) %}
  {%- if var('telemetry_source', none) -%}
    {{ alias }}.source = '{{ var("telemetry_source") }}'
  {%- else -%}
    true
  {%- endif -%}
{% endmacro %}
