"""Tests for environment-driven configuration parsing."""

from __future__ import annotations

import pytest

from energy_platform.config import PostgresConfig, SmardConfig


def test_defaults_need_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENERGY_PG_PORT", "DAGSTER_POSTGRES_PORT", "ENERGY_SMARD_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    assert PostgresConfig.from_env().port == 5432
    assert SmardConfig.from_env().timeout_seconds == 30.0


def test_malformed_int_raises_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_PG_PORT", "not-a-port")
    with pytest.raises(ValueError, match="ENERGY_PG_PORT"):
        PostgresConfig.from_env()


def test_malformed_float_raises_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_SMARD_TIMEOUT", "soon")
    with pytest.raises(ValueError, match="ENERGY_SMARD_TIMEOUT"):
        SmardConfig.from_env()
