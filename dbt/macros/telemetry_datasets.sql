{#
  The datasets a household telemetry connector writes, as a Jinja list.

  Lives in a macro because three places need the same list and none of them may drift from the
  others: stg_telemetry pivots exactly these into its wide row, and
  assert_telemetry_rows_declare_a_data_mode uses them to decide which raw rows are telemetry and
  must therefore carry a data_mode. A dataset added to the pivot but not to the guard would be
  telemetry the mode column silently ignores.

  Emits a quoted, comma-separated list for use inside an IN predicate:

      where o.dataset in ({{ telemetry_datasets() }})
#}
{% macro telemetry_datasets() %}
    {%- set datasets = [
        'pv_production',
        'household_load',
        'load_base',
        'ac_power',
        'indoor_temperature',
        'battery_charge',
        'battery_discharge',
        'soc',
        'grid_import',
        'grid_export',
    ] -%}
    {%- for dataset in datasets -%}
        '{{ dataset }}'{{ ", " if not loop.last }}
    {%- endfor -%}
{% endmacro %}
