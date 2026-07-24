"""Record real Open-Meteo responses as test fixtures (dev-only; not run in CI).

Fetches the live archive for a handful of representative Europe/Berlin days -- including both
2024 DST-transition Sundays -- plus a current forecast snapshot, and writes them verbatim under
``tests/connectors/fixtures/`` with the filenames the offline ``MockTransport`` expects. The
archive requests reuse the client's own parameter builder so the recorded date-ranges never
drift from what production code actually asks for.

Run with: ``uv run python scripts/record_open_meteo_fixtures.py``
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

# The private helpers are imported deliberately (as the SMARD recorder reuses
# ``SmardClient._weeks_covering``) so fixtures stay consistent with the client's requests.
from energy_platform.connectors.open_meteo import (
    DEFAULT_ARCHIVE_URL,
    DEFAULT_FORECAST_URL,
    USER_AGENT,
    _archive_params,
    _forecast_params,
    _utc_date,
)
from energy_platform.orchestration.ingest import berlin_day_window

COORDS = (52.52, 13.40)  # rounded site coordinates -- the only ones in the repo
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "connectors" / "fixtures"

# Representative days: a normal day plus both DST-transition Sundays.
DAYS: list[date] = [date(2024, 6, 12), date(2024, 3, 31), date(2024, 10, 27)]
FORECAST_DAYS = 7
PAST_DAYS = 1


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    lat, lon = COORDS
    with httpx.Client(
        timeout=30.0, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    ) as http:
        seen: set[tuple[str, str]] = set()
        for day in DAYS:
            window = berlin_day_window(day)
            start_date = _utc_date(window.start_ms)
            end_date = _utc_date(window.end_ms - 1)
            if (start_date.isoformat(), end_date.isoformat()) in seen:
                continue
            seen.add((start_date.isoformat(), end_date.isoformat()))

            params = _archive_params(lat, lon, start_date, end_date)
            raw = http.get(DEFAULT_ARCHIVE_URL, params=params).content
            out = FIXTURES / f"open_meteo_archive_{start_date}_{end_date}.json"
            out.write_bytes(raw)
            print(f"wrote {out.name} ({len(raw)} bytes)")

        forecast_params = _forecast_params(lat, lon, FORECAST_DAYS, PAST_DAYS)
        raw = http.get(DEFAULT_FORECAST_URL, params=forecast_params).content
        out = FIXTURES / "open_meteo_forecast.json"
        out.write_bytes(raw)
        print(f"wrote {out.name} ({len(raw)} bytes)")


if __name__ == "__main__":
    main()
