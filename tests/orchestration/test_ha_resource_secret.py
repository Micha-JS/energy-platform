"""The Home Assistant token must never be baked into serialized Dagster resource config.

Guards the *invariant* (credentials come only from the environment, are never persisted or shown
in the launchpad UI), not just the one call site: with the token wired as an ``EnvVar``, the
serialized config carries only the variable *name*, while runtime resolution still yields the
secret. A regression to a resolved token value -- either on the resource or in ``definitions`` --
fails here.
"""

from __future__ import annotations

import pytest
from dagster import EnvVar

import energy_platform.definitions as definitions
from energy_platform.orchestration.resources import HomeAssistantResource

_SENTINEL = "sentinel-super-secret-token-value"


def _resource() -> HomeAssistantResource:
    return HomeAssistantResource(
        enabled=True,
        base_url="https://ha.example",
        token=EnvVar("ENERGY_HA_TOKEN"),
        entity_map={"pv_production": "sensor.pv_power"},
    )


def test_token_value_absent_from_serialized_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_HA_TOKEN", _SENTINEL)
    dumped = _resource().model_dump_json()
    # Only the env var *name* is persisted into run storage -- never the secret value.
    assert _SENTINEL not in dumped
    assert "ENERGY_HA_TOKEN" in dumped


def test_token_resolves_from_env_at_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_HA_TOKEN", _SENTINEL)
    initialized = _resource().process_config_and_initialize()
    assert initialized.token == _SENTINEL


def test_definitions_wire_the_token_as_an_env_var() -> None:
    # Call-site guard: the code location must configure the token as an EnvVar, so nothing resolves
    # (and could be serialized) at definition time.
    assert isinstance(definitions.defs.resources["home_assistant"].token, EnvVar)
