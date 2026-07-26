"""Record real SMARD responses as test fixtures (dev-only; not run in CI).

Fetches the live SMARD index and the weekly files covering the days the offline test suite and
the M4 CI backfill exercise -- a normal day plus the two ~week-long windows straddling the 2024
DST-transition Sundays -- and writes them verbatim under ``tests/connectors/fixtures/``. Because
SMARD serves whole weeks, covering a window's endpoints pulls every day in between for free. The
stored index is trimmed to only the weeks fetched so the offline ``MockTransport`` is
self-consistent. Provenance is tracked in ``tests/connectors/fixtures/MANIFEST.md``.

Run with: ``uv run python scripts/record_smard_fixtures.py``
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx

# The two M4 CI windows plus a normal reference day are the shared fixture contract; endpoints
# suffice for SMARD's weekly files (both DST Sundays fall inside their window).
from _fixture_windows import MARCH_WINDOW, NORMAL_DAY, OCTOBER_WINDOW, expand_days

from energy_platform.connectors.smard import SmardClient
from energy_platform.connectors.types import Resolution
from energy_platform.orchestration.ingest import berlin_day_window

BASE_URL = "https://www.smard.de/app/chart_data"
REGION = "DE"
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "connectors" / "fixtures"

_WINDOW_DAYS = [NORMAL_DAY, *expand_days(MARCH_WINDOW, OCTOBER_WINDOW)]

# (filter_id, resolution, representative days to cover). Hourly spans both full windows; the
# quarter-hour series only needs the DST Sundays the unit tests assert on.
SPECS: list[tuple[str, Resolution, list[date]]] = [
    ("4169", Resolution.HOUR, _WINDOW_DAYS),
    ("410", Resolution.HOUR, _WINDOW_DAYS),
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
                out = FIXTURES / f"{filter_id}_{REGION}_{resolution.value}_{week_ts}.json"
                # Additive: never clobber an already-recorded fixture (some carry a hand-injected
                # gap for the "NaN over fabrication" tests). Delete a file to force a refresh.
                if out.exists():
                    print(f"skip  {out.name} (exists)")
                    continue
                raw = http.get(_week_url(filter_id, resolution, week_ts)).content
                out.write_bytes(raw)
                print(f"wrote {out.name} ({len(raw)} bytes)")

            trimmed = FIXTURES / f"{filter_id}_{REGION}_index_{resolution.value}.json"
            trimmed.write_text(json.dumps({"timestamps": weeks}), encoding="utf-8")
            print(f"wrote {trimmed.name} ({len(weeks)} weeks)")


if __name__ == "__main__":
    main()
