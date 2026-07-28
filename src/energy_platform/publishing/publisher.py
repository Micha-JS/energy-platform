"""The shared core: turn one day's stored plan into messages, and decide whether to send them.

Driven identically by the CLI and by the Dagster job, exactly as ``ingest_partition`` is -- the
function takes an already-opened reader, an injected publisher and a ledger, and never reads the
environment or opens a socket itself. That is what lets the whole decision path be tested in
process against :class:`~energy_platform.publishing.client.RecordingPublisher`.

**Nothing here computes a plan.** M8 decided it and the warehouse holds it; this assembles a
payload out of stored columns. If a number is missing from a message, the fix is upstream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from energy_platform.publishing.client import Message, MqttPublisher
from energy_platform.publishing.contract import PlanPayload, build_payload, plan_topic
from energy_platform.publishing.discovery import HEADS, discovery_config, discovery_topic
from energy_platform.publishing.reader import ForwardPlanReader
from energy_platform.publishing.store import PublicationRecord, PublicationRepository


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """What one publish call did, in the shape the CLI reports and a test asserts on."""

    payload: PlanPayload
    topic: str
    messages: tuple[Message, ...]
    published: bool
    reason: str

    @property
    def message_count(self) -> int:
        return len(self.messages)


def publish_plan(
    reader: ForwardPlanReader,
    publisher: MqttPublisher,
    ledger: PublicationRepository | None,
    *,
    site_id: str,
    day: date,
    tariff_id: str,
    now: datetime,
    topic_prefix: str,
    qos: int,
    retain: bool,
    discovery_prefix: str | None,
) -> PublishOutcome:
    """Read the day's plan, build the payload, and publish it unless it is already out there.

    ``ledger`` is optional so ``--dry-run`` can assemble and show the exact payload without
    touching the database it would otherwise write to. ``discovery_prefix`` is ``None`` when
    discovery is switched off; the plan document is published either way, because the document is
    the contract and the discovery entities are a convenience layered on it.

    ``now`` is passed in rather than read from the clock, for the reason every other timestamp in
    this codebase is: a function that calls ``datetime.now()`` cannot be asserted against.
    """
    plan = reader.read_day(site_id, day, tariff_id)
    payload = build_payload(
        site_id=site_id,
        tariff_id=tariff_id,
        issued_at=plan.decision_time,
        published_at=now,
        plan_status=plan.plan_status,
        coverage=plan.coverage,
        hours=plan.hours,
        provenance=plan.provenance,
    )
    topic = plan_topic(topic_prefix, site_id)
    digest = payload.digest()

    previous = None if ledger is None else ledger.last_digest(site_id, day, tariff_id, topic)
    if previous == digest:
        # The retained message on the broker is already exactly this plan. Re-sending would be
        # harmless -- retention overwrites -- but reporting it as a publication would make the
        # ledger's `published_at` drift on every schedule tick and hide the fact that the plan has
        # not actually changed since it was first issued.
        return PublishOutcome(
            payload=payload,
            topic=topic,
            messages=(),
            published=False,
            reason="unchanged: the retained plan already carries this exact payload",
        )

    messages = [Message(topic=topic, payload=payload.to_json(), qos=qos, retain=retain)]
    if discovery_prefix is not None:
        messages.extend(
            Message(
                topic=discovery_topic(discovery_prefix, site_id, head.key),
                payload=json.dumps(
                    discovery_config(head, site_id=site_id, state_topic=topic),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                qos=qos,
                # Retained for the same reason the plan is: Home Assistant re-reads discovery on
                # restart, and an unretained config would leave the entities unregistered until
                # the next publish.
                retain=retain,
            )
            for head in HEADS
        )

    # The plan document goes first. If the process dies between messages, a house that has the plan
    # but not the discovery entities is in a strictly better state than one that has entities
    # pointing at a topic with nothing on it.
    for message in messages:
        publisher.publish(message)

    if ledger is not None:
        ledger.record(
            PublicationRecord(
                site_id=site_id,
                local_date=day,
                tariff_id=tariff_id,
                topic=topic,
                payload_digest=digest,
                schema_version=payload.schema_version,
                input_digest=plan.provenance.input_digest,
                published_at=now,
            )
        )

    return PublishOutcome(
        payload=payload,
        topic=topic,
        messages=tuple(messages),
        published=True,
        reason="published" if previous is None else "republished: the plan changed",
    )
