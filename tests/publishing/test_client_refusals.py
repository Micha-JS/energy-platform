"""Disabled by default, and loud about it.

The publisher is the one component that reaches *out* of the platform, so the failure to guard
against is not a crash -- it is a public repo or a CI run quietly connecting to somebody's broker.
These assert the two refusals fire before any socket is opened, which is the same posture (and the
same two-stage shape) as the real Home Assistant connector's.
"""

from __future__ import annotations

import pytest

from energy_platform.config import MqttConfig
from energy_platform.publishing.client import MqttPublishError, connect


def test_disabled_by_default() -> None:
    # The default-constructed config is the one a fresh clone gets.
    assert MqttConfig().enabled is False
    with pytest.raises(MqttPublishError, match="ENERGY_MQTT_ENABLED"), connect(MqttConfig()):
        pass


def test_enabled_without_a_host_is_a_different_message() -> None:
    """Two refusals, not one. 'Turn it on' and 'you turned it on but did not finish' need
    different fixes, and a merged message sends the reader to the wrong place."""
    with (
        pytest.raises(MqttPublishError, match="ENERGY_MQTT_HOST must be set"),
        connect(MqttConfig(enabled=True, host="")),
    ):
        pass


def test_the_refusal_happens_before_any_connection_attempt() -> None:
    # A disabled publisher pointed at an unroutable host must fail on `enabled`, not on a timeout:
    # if it ever tried to connect first, CI would hang instead of failing.
    with (
        pytest.raises(MqttPublishError, match="disabled"),
        connect(MqttConfig(enabled=False, host="192.0.2.1", port=1883)),
    ):
        pass


def test_env_parsing_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_MQTT_ENABLED", "1")
    monkeypatch.setenv("ENERGY_MQTT_HOST", "mosquitto")
    monkeypatch.setenv("ENERGY_MQTT_PORT", "8883")
    monkeypatch.setenv("ENERGY_MQTT_TLS", "1")
    monkeypatch.setenv("ENERGY_MQTT_TOPIC_PREFIX", "house")
    monkeypatch.setenv("ENERGY_MQTT_RETAIN", "0")

    config = MqttConfig.from_env()
    assert (config.enabled, config.host, config.port) == (True, "mosquitto", 8883)
    assert config.tls is True
    assert config.topic_prefix == "house"
    assert config.retain is False


def test_retain_and_discovery_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retention is not a tuning knob: without it a Home Assistant restart loses the plan until the
    # next schedule tick, which on a daily schedule can be most of a day.
    for var in ("ENERGY_MQTT_RETAIN", "ENERGY_MQTT_DISCOVERY_ENABLED", "ENERGY_MQTT_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    config = MqttConfig.from_env()
    assert config.retain is True
    assert config.discovery_enabled is True
    assert config.enabled is False  # ...but publishing itself still is not
