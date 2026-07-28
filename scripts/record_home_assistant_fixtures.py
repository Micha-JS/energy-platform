"""Record a real Home Assistant history response as a test fixture (dev-only; not run in CI).

Fetches one entity's state history for a representative day from a live Home Assistant instance
and writes it, **credential-free**, under ``tests/connectors/fixtures/`` for the offline
``MockTransport`` tests. The request reuses the client's own URL/param builders so the recorded
shape never drifts from what production asks for.

Read-only: this only ever GETs the history endpoint. Run from a trusted machine (the Fenecon is
reachable over Tailscale) with credentials in the environment -- never commit the token:

    ENERGY_HA_URL=https://ha.example ENERGY_HA_TOKEN=... \
        uv run python scripts/record_home_assistant_fixtures.py pv_production sensor.pv_power

The first argument is the :class:`Dataset` to record (it selects both the client's conversion
path and the fixture filename), the second the entity id. The committed fixtures are small
hand-sanitised samples; regenerate only if the Home Assistant history schema changes.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import httpx

from energy_platform.connectors.home_assistant import USER_AGENT, HomeAssistantClient
from energy_platform.connectors.types import Dataset
from energy_platform.orchestration.ingest import berlin_day_window

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "connectors" / "fixtures"
DAY = date(2024, 6, 15)  # a clear summer day


def main() -> None:
    dataset = Dataset(sys.argv[1]) if len(sys.argv) > 1 else Dataset.PV_PRODUCTION
    entity = sys.argv[2] if len(sys.argv) > 2 else "sensor.pv_power"
    base_url = os.environ.get("ENERGY_HA_URL")
    token = os.environ.get("ENERGY_HA_TOKEN")
    if not base_url or not token:
        raise SystemExit("set ENERGY_HA_URL and ENERGY_HA_TOKEN in the environment")

    FIXTURES.mkdir(parents=True, exist_ok=True)
    window = berlin_day_window(DAY)
    with httpx.Client(
        timeout=30.0,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    ) as http:
        # Build the exact request the client would issue (URL + params), then GET it directly so
        # the recorded body matches production without exposing the token in the fixture.
        client = HomeAssistantClient(http, base_url, {dataset: entity})
        url, params = client.build_history_request(entity, window)
        raw = http.get(url, params=params).content

    out = FIXTURES / f"home_assistant_{dataset.value}.json"
    out.write_bytes(raw)
    print(f"wrote {out.name} ({len(raw)} bytes) -- review and sanitise before committing")


if __name__ == "__main__":
    main()
