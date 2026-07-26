"""Tests for environment-driven configuration parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from energy_platform.config import (
    BatteryConfig,
    HomeAssistantConfig,
    OpenMeteoConfig,
    PostgresConfig,
    PvSystemConfig,
    SiteConfig,
    SmardConfig,
    TariffConfig,
)
from energy_platform.tariffs.catalog import load_catalog


def test_defaults_need_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENERGY_PG_PORT", "DAGSTER_POSTGRES_PORT", "ENERGY_SMARD_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    assert PostgresConfig.from_env().port == 5432
    assert SmardConfig.from_env().timeout_seconds == 30.0


def test_site_defaults_are_the_rounded_public_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENERGY_SITE_ID", "ENERGY_SITE_LAT", "ENERGY_SITE_LON"):
        monkeypatch.delenv(var, raising=False)
    site = SiteConfig.from_env().default
    assert site.id == "home"
    # Rounded to 2 dp -- the only coordinates in the repo.
    assert site.latitude == 52.52
    assert site.longitude == 13.40


def test_site_coordinates_are_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_SITE_LAT", "48.14")
    monkeypatch.setenv("ENERGY_SITE_LON", "11.58")
    site = SiteConfig.from_env().default
    assert (site.latitude, site.longitude) == (48.14, 11.58)


def test_unknown_site_resolution_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(KeyError, match="unknown site"):
        SiteConfig.from_env().resolve("atlantis")


def test_open_meteo_forecast_days_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENERGY_FORECAST_DAYS", raising=False)
    assert OpenMeteoConfig.from_env().forecast_days == 7


def test_malformed_int_raises_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_PG_PORT", "not-a-port")
    with pytest.raises(ValueError, match="ENERGY_PG_PORT"):
        PostgresConfig.from_env()


def test_malformed_float_raises_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_SMARD_TIMEOUT", "soon")
    with pytest.raises(ValueError, match="ENERGY_SMARD_TIMEOUT"):
        SmardConfig.from_env()


def test_system_defaults_match_the_fenecon(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENERGY_PV_DC_KWP", "ENERGY_BATTERY_CAPACITY_KWH", "ENERGY_BATTERY_RTE"):
        monkeypatch.delenv(var, raising=False)
    assert PvSystemConfig.from_env().dc_kwp == 8.8
    battery = BatteryConfig.from_env()
    assert battery.capacity_kwh == 14.0
    assert 0.0 < battery.round_trip_efficiency <= 1.0
    # AC cap is a distinct inverter property, not the DC nameplate.
    assert PvSystemConfig.from_env().ac_cap_kw != PvSystemConfig.from_env().dc_kwp


def test_tariff_defaults_name_catalogue_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENERGY_TARIFF_CATALOG", "ENERGY_TARIFF_ID", "ENERGY_FEED_IN_TARIFF_ID"):
        monkeypatch.delenv(var, raising=False)
    config = TariffConfig.from_env()
    assert config.catalog_path == ""  # unset: the committed seed is used
    # Ids, never rates -- every rate lives in the catalogue the dbt seed and the engine share.
    catalog = load_catalog()
    assert config.consumption_tariff_id in catalog
    assert config.feed_in_tariff_id in catalog


def test_tariff_ids_are_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_TARIFF_ID", "dynamic_2024")
    assert TariffConfig.from_env().consumption_tariff_id == "dynamic_2024"


def test_tariff_feed_in_default_matches_the_dbt_var() -> None:
    """The dbt var feed_in_tariff_id and this default must name the same scheme, or the SQL marts
    and the Python engine would compensate exports at different rates."""
    project = (Path(__file__).resolve().parents[1] / "dbt" / "dbt_project.yml").read_text()
    assert f'feed_in_tariff_id: "{TariffConfig.from_env().feed_in_tariff_id}"' in project


def test_home_assistant_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENERGY_HA_ENABLED", "ENERGY_HA_URL", "ENERGY_HA_TOKEN", "ENERGY_HA_ENTITY_MAP"):
        monkeypatch.delenv(var, raising=False)
    config = HomeAssistantConfig.from_env()
    assert config.enabled is False
    assert config.entities == {}


def test_home_assistant_entity_map_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_HA_ENABLED", "yes")
    monkeypatch.setenv("ENERGY_HA_ENTITY_MAP", "pv_production=sensor.pv, soc=sensor.soc")
    config = HomeAssistantConfig.from_env()
    assert config.enabled is True
    assert config.entities == {"pv_production": "sensor.pv", "soc": "sensor.soc"}


def test_home_assistant_malformed_entity_map_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_HA_ENTITY_MAP", "pv_production")  # no '=entity'
    with pytest.raises(ValueError, match="ENERGY_HA_ENTITY_MAP"):
        HomeAssistantConfig.from_env()


def test_bool_env_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_HA_ENABLED", "maybe")
    with pytest.raises(ValueError, match="ENERGY_HA_ENABLED"):
        HomeAssistantConfig.from_env()
