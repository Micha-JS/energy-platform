"""Tariff engine: static / dynamic (day-ahead) / feed-in compensation.

Parameters come from one committed CSV (:mod:`~energy_platform.tariffs.catalog`) that dbt loads
as a seed and this package reads directly, so the two layers can never disagree about the
numbers. The arithmetic (:mod:`~energy_platform.tariffs.engine`) is mirrored by the dbt macro
``tariff_import_price_ct_kwh`` and pinned to it by a reconciliation test.
"""

from energy_platform.tariffs.catalog import (
    CATALOG_PATH_ENV,
    DEFAULT_CATALOG_PATH,
    TariffKind,
    TariffSpec,
    catalog_path,
    load_catalog,
    resolve,
)
from energy_platform.tariffs.counterfactual import (
    CounterfactualFlows,
    no_battery_flows,
    self_consumed_kwh,
)
from energy_platform.tariffs.engine import (
    CT_PER_EUR,
    EUR_PER_MWH_PER_CT_PER_KWH,
    base_fee_eur,
    energy_cost_eur,
    feed_in_revenue_eur,
    import_price_ct_kwh,
    spot_ct_kwh,
)

__all__ = [
    "CATALOG_PATH_ENV",
    "CT_PER_EUR",
    "DEFAULT_CATALOG_PATH",
    "EUR_PER_MWH_PER_CT_PER_KWH",
    "CounterfactualFlows",
    "TariffKind",
    "TariffSpec",
    "base_fee_eur",
    "catalog_path",
    "energy_cost_eur",
    "feed_in_revenue_eur",
    "import_price_ct_kwh",
    "load_catalog",
    "no_battery_flows",
    "resolve",
    "self_consumed_kwh",
    "spot_ct_kwh",
]
