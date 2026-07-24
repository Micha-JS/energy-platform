"""Tests for environment-driven configuration parsing."""

from __future__ import annotations

import pytest

from energy_platform.config import OpenMeteoConfig, PostgresConfig, SiteConfig, SmardConfig


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
