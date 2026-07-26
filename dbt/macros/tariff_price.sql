{#
  The SQL half of the tariff arithmetic -- the retail price of one imported kWh, in ct/kWh,
  gross of VAT.

  This is the single place the formula lives in SQL, so every model that prices energy shares
  one expression. Its Python twin is `energy_platform.tariffs.engine.import_price_ct_kwh`, and
  the two are pinned together by tests/dbt/test_tariff_reconciliation.py, which recomputes every
  priced hour of int_hourly_tariff_cost with the Python engine and asserts equality. Editing
  this macro without editing the engine (or the reverse) fails CI.

  Units: the warehouse stores wholesale price in EUR/MWh, retail tariffs are quoted in ct/kWh,
  and the conversion is a division by TEN, not a thousand:

      100 EUR/MWh  ==  10 ct/kWh  ==  0.10 EUR/kWh

  A /1000 here would be silent -- the numbers stay plausible, just 10x too small -- so the
  conversion is asserted explicitly by a dbt unit test as well as by the reconciliation.

  VAT: every per-kWh parameter in the catalogue is NET. The rate is applied exactly once, here.

  Negative prices: day-ahead prices go negative and nothing clamps them. A dynamic import price
  below zero means the household is paid to consume -- arithmetic, not an error.

  Arithmetic runs in NUMERIC and the result is cast back to double precision.

  Exact decimal is what money wants: computed in double precision this expression returns
  13.316099999999997 where 13.3161 was meant, and that noise then accumulates through every sum in
  the marts. The spot price arrives as a double (the raw zone stores every observation that way)
  so it is cast on the way in; the catalogue columns are already numeric by declared column type.

  The cast back keeps the column type consistent with every other numeric column in the warehouse,
  and -- because the decimal value is exact -- lands on precisely the double a human writing
  13.3161 would get, which is what lets the dbt unit tests assert readable values.

  The Python engine stays in float throughout, because M6's optimiser needs floats. The two
  therefore agree to within a rounding step rather than bit for bit, and the reconciliation test
  compares with a tolerance.
#}
{% macro tariff_import_price_ct_kwh(tariff_alias, spot_col) %}
    (case {{ tariff_alias }}.kind
        when 'static' then
            {{ tariff_alias }}.price_ct_kwh * (1 + {{ tariff_alias }}.vat_rate)
        when 'dynamic' then
            -- NULL spot propagates: a dynamic tariff has no price for an unpriced hour.
            ({{ spot_col }}::numeric / {{ eur_mwh_per_ct_kwh() }}
                + {{ tariff_alias }}.margin_ct_kwh
                + {{ tariff_alias }}.pass_through_ct_kwh)
            * (1 + {{ tariff_alias }}.vat_rate)
    end)::float8
{% endmacro %}


{#
  EUR/MWh per ct/kWh. One EUR is 100 ct and one MWh is 1000 kWh, so ten EUR/MWh make one ct/kWh:
  the conversion DIVIDES by 10.

  Written as a division by an exact integer rather than a multiplication by 0.1 deliberately. 0.1
  has no exact binary representation, so `spot * 0.1` injects rounding noise into every price --
  visible as -12.863900000000003 where -12.8639 was meant, which then has to be either asserted or
  tolerated in every test. Dividing by 10 is exact for these magnitudes, on both sides of the
  Python/SQL boundary, so the two implementations agree bit for bit and the unit tests can compare
  exactly. Mirrors EUR_PER_MWH_PER_CT_PER_KWH in energy_platform.tariffs.engine.
#}
{% macro eur_mwh_per_ct_kwh() %}10{% endmacro %}
