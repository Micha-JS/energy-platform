"""The broker surface: a Protocol the publisher speaks, and the paho client behind it.

**The transport is injected, not constructed in the core.** Same shape as
:class:`~energy_platform.connectors.home_assistant.HomeAssistantClient`, which takes an
already-authenticated ``httpx.Client`` -- and for the same payoff: the publisher's logic is
testable in-process against :class:`RecordingPublisher` with no broker, no sockets, and no sleep,
while the one thing a fake genuinely cannot vouch for (that the wire format reaches a real broker
with ``retain`` honoured and survives a reconnect) is covered by a single integration test against
a real Mosquitto.

**Disabled by default, credentials from the environment only.** Two separate refusals with two
distinct messages -- "you have not enabled this" and "you enabled it but did not finish
configuring it" -- because they call for different fixes and a merged message would send the reader
looking in the wrong place.

``paho`` is imported inside :func:`connect` rather than at module scope: ``energy_platform.cli``
and the Dagster code location both import this package's siblings, and neither should pay for an
MQTT stack to run a backfill.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from energy_platform.config import MqttConfig

if TYPE_CHECKING:
    # Type-only, so the annotation is real without paho landing in the runtime import graph --
    # the same device cli.py uses to type a ForwardSolution it must not import.
    from paho.mqtt.client import Client as PahoClient


class MqttPublishError(RuntimeError):
    """Raised when the broker is unusable: disabled, unconfigured, or unreachable."""


@dataclass(frozen=True, slots=True)
class Message:
    """One message, as published. The retain flag is data, so a test can assert on it."""

    topic: str
    payload: str
    qos: int
    retain: bool


@runtime_checkable
class MqttPublisher(Protocol):
    """Everything the publisher needs from a broker. Deliberately one method.

    Narrow on purpose: a publisher that could subscribe would eventually be asked to read state
    back and reconcile it, and this platform's whole claim is that it emits advice and never
    negotiates with the house.
    """

    def publish(self, message: Message) -> None: ...


class RecordingPublisher:
    """An in-process :class:`MqttPublisher` that records instead of sending.

    Product code rather than a test shim, for the same reason
    :mod:`energy_platform.connectors.offline` is: ``publish-plan --dry-run`` uses it to show
    exactly what would go on the wire, which makes "what will this tell my house?" answerable
    without a broker and without publishing.
    """

    def __init__(self) -> None:
        self.messages: list[Message] = []

    def publish(self, message: Message) -> None:
        self.messages.append(message)


class PahoPublisher:
    """A real broker, wrapping a connected paho client."""

    def __init__(self, client: PahoClient, *, timeout_seconds: float) -> None:
        self._client = client
        self._timeout = timeout_seconds

    def publish(self, message: Message) -> None:
        info = self._client.publish(
            message.topic, message.payload, qos=message.qos, retain=message.retain
        )
        # QoS 1 without waiting for the broker's PUBACK would let the process exit with the message
        # still in paho's out-queue, and the ledger would then record a plan the house never got.
        info.wait_for_publish(timeout=self._timeout)
        if not info.is_published():
            raise MqttPublishError(
                f"the broker did not acknowledge {message.topic} within {self._timeout}s"
            )


@contextmanager
def connect(config: MqttConfig) -> Iterator[MqttPublisher]:
    """Open a broker connection, refusing loudly when this has not been set up.

    The two refusals are separate because the fixes are: one is "turn it on", the other is "you
    turned it on but the host is missing". Credentials are optional -- an anonymous listener on a
    trusted LAN is a legitimate deployment, and demanding a password would push people towards
    putting one in a compose file.
    """
    if not config.enabled:
        raise MqttPublishError(
            "the MQTT plan publisher is disabled; set ENERGY_MQTT_ENABLED=1 with "
            "ENERGY_MQTT_HOST to publish plans. Nothing is published by default: this is the one "
            "component that reaches out of the platform."
        )
    if not config.host:
        raise MqttPublishError("ENERGY_MQTT_HOST must be set when the plan publisher is enabled.")

    # Imported here, not at module scope -- see the module docstring.
    from paho.mqtt.client import Client
    from paho.mqtt.enums import CallbackAPIVersion

    client = Client(CallbackAPIVersion.VERSION2, client_id=config.client_id)
    if config.username:
        client.username_pw_set(config.username, config.password or None)
    if config.tls:
        client.tls_set()
    try:
        client.connect(config.host, config.port, keepalive=int(config.timeout_seconds) or 10)
    except OSError as exc:
        raise MqttPublishError(
            f"could not reach the MQTT broker at {config.host}:{config.port}: {exc}"
        ) from exc
    client.loop_start()
    try:
        yield PahoPublisher(client, timeout_seconds=config.timeout_seconds)
    finally:
        client.loop_stop()
        client.disconnect()
