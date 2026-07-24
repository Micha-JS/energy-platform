"""Record real SMARD responses as test fixtures (dev-only; not run in CI).

Fetches the live SMARD index and the weekly files covering a handful of representative
days -- including both 2024 DST-transition Sundays -- and writes them verbatim under
``tests/connectors/fixtures/``. The stored index is trimmed to only the weeks fetched so
the offline ``MockTransport`` in the test suite is self-consistent.

Run with: ``uv run python scripts/record_smard_fixtures.py``
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx

from energy_platform.connectors.smard import SmardClient
from energy_platform.connectors.types import Resolution
from energy_platform.orchestration.ingest import berlin_day_window

BASE_URL = "https://www.smard.de/app/chart_data"
REGION = "DE"
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "connectors" / "fixtures"

# (filter_id, resolution, representative days to cover)
SPECS: list[tuple[str, Resolution, list[date]]] = [
    ("4169", Resolution.HOUR, [date(2024, 6, 12), date(2024, 3, 31), date(2024, 10, 27)]),
    ("410", Resolution.HOUR, [date(2024, 6, 12), date(2024, 3, 31), date(2024, 10, 27)]),
    ("4169", Resolution.QUARTERHOUR, [date(2024, 3, 31), date(2024, 10, 27)]),
]


def _week_url(filter_id: str, resolution: Resolution, week_ts: int) -> str:
    return f"{BASE_URL}/{filter_id}/{REGION}/{filter_id}_{REGION}_{resolution.value}_{week_ts}.json"


def _weeks_for_days(
    client: SmardClient, filter_id: str, resolution: Resolution, days: list[date]
) -> set[int]:
    """Union of weekly files the client itself would request to cover ``days``.

    Delegates to :meth:`SmardClient._weeks_covering` rather than re-deriving the coverage
    rule, so recorded fixtures never drift from the weeks production code actually fetches.
    """
    weeks: set[int] = set()
    for day in days:
        window = berlin_day_window(day)
        weeks.update(client._weeks_covering(filter_id, REGION, resolution, window))
    return weeks


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30.0, headers={"Accept": "application/json"}) as http:
        client = SmardClient(http, base_url=BASE_URL)
        for filter_id, resolution, days in SPECS:
            weeks = sorted(_weeks_for_days(client, filter_id, resolution, days))

            for week_ts in weeks:
                raw = http.get(_week_url(filter_id, resolution, week_ts)).content
                out = FIXTURES / f"{filter_id}_{REGION}_{resolution.value}_{week_ts}.json"
                out.write_bytes(raw)
                print(f"wrote {out.name} ({len(raw)} bytes)")

            trimmed = FIXTURES / f"{filter_id}_{REGION}_index_{resolution.value}.json"
            trimmed.write_text(json.dumps({"timestamps": weeks}), encoding="utf-8")
            print(f"wrote {trimmed.name} ({len(weeks)} weeks)")


if __name__ == "__main__":
    main()
