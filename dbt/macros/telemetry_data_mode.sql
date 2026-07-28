{#
  Which kind of household telemetry the warehouse was built from, derived from the connector that
  wrote the rows: 'demo_synthetic' for the M3 generator, 'real' for the Fenecon reader.

  M9's dashboard banner has to state the active data mode, and it is not allowed to decide that
  for itself -- the mart-only rule means a screen figure is a mart column, so the classification
  is made once here in SQL rather than by a Python string comparison in the presentation layer.

  Sourced from the connector name because that is the only provenance the raw zone carries:
  raw.ingestion.source, written from the module constants in
  src/energy_platform/connectors/{synthetic,home_assistant}.py. The market and weather connectors
  ('smard', 'open_meteo') are not telemetry and map to NULL rather than being forced into one of
  the two modes -- a price series is neither synthetic nor real household data.

  NULL is therefore correct for a non-telemetry row and a bug for a telemetry one, which is
  exactly what assert_telemetry_rows_declare_a_data_mode checks: a third telemetry connector added
  without a branch here fails that test instead of quietly reporting an unknown mode.
#}
{% macro telemetry_data_mode(source_col) %}
    case {{ source_col }}
        when 'synthetic' then 'demo_synthetic'
        when 'fenecon'   then 'real'
    end
{% endmacro %}
