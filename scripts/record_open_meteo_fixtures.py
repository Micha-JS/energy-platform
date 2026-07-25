"""Record real Open-Meteo responses as test fixtures (dev-only; not run in CI).

Fetches the live archive for a handful of representative Europe/Berlin days -- including both
2024 DST-transition Sundays -- plus a current forecast snapshot, and writes them verbatim under
``tests/connectors/fixtures/`` with the filenames the offline ``MockTransport`` expects. The
archive requests reuse the client's own parameter builder so the recorded date-ranges never
drift from what production code actually asks for.

Run with: ``uv run python scripts/record_open_meteo_fixtures.py``
"""

from __future__ import annotations

from datetime import date, timedelta
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

# The archive is requested one Berlin-day window at a time, so every day the M4 CI backfill loads
# needs its own recorded window. Cover a normal reference day plus the two ~week-long windows
# straddling the 2024 DST-transition Sundays.
NORMAL_DAY = date(2024, 6, 12)
MARCH_WINDOW = (date(2024, 3, 28), date(2024, 4, 3))
OCTOBER_WINDOW = (date(2024, 10, 24), date(2024, 10, 30))


def _span_days(*spans: tuple[date, date]) -> list[date]:
    out: list[date] = []
    for start, end in spans:
        day = start
        while day <= end:
            out.append(day)
            day += timedelta(days=1)
    return out


DAYS: list[date] = [NORMAL_DAY, *_span_days(MARCH_WINDOW, OCTOBER_WINDOW)]
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

            out = FIXTURES / f"open_meteo_archive_{start_date}_{end_date}.json"
            # Additive: never clobber an already-recorded fixture (the reference day and the
            # forecast carry a hand-injected gap for the "NaN over fabrication" tests). Delete a
            # file to force a refresh.
            if out.exists():
                print(f"skip  {out.name} (exists)")
                continue
            params = _archive_params(lat, lon, start_date, end_date)
            raw = http.get(DEFAULT_ARCHIVE_URL, params=params).content
            out.write_bytes(raw)
            print(f"wrote {out.name} ({len(raw)} bytes)")

        forecast_out = FIXTURES / "open_meteo_forecast.json"
        if forecast_out.exists():
            print(f"skip  {forecast_out.name} (exists)")
        else:
            forecast_params = _forecast_params(lat, lon, FORECAST_DAYS, PAST_DAYS)
            raw = http.get(DEFAULT_FORECAST_URL, params=forecast_params).content
            forecast_out.write_bytes(raw)
            print(f"wrote {forecast_out.name} ({len(raw)} bytes)")


if __name__ == "__main__":
    main()
