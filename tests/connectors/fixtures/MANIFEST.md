# Recorded fixtures — provenance

These JSON files are verbatim SMARD and Open-Meteo API responses, served offline by
`energy_platform.connectors.offline` for the unit tests and for `--offline` runs of the
`energy-platform` CLI (M4 CI seeds the warehouse this way — no live API calls).

Re-record with `uv run python scripts/record_smard_fixtures.py` and
`uv run python scripts/record_open_meteo_fixtures.py`. Both recorders are **additive**: an
existing file is never overwritten (delete it first to force a refresh), so the curated fixtures
below keep their hand-injected gaps. The index files are always rewritten to the union of weeks
covered.

| Group | Files | Coverage (Europe/Berlin) | Source | Recorded |
|---|---|---|---|---|
| SMARD prices/load (hourly) | `4169_DE_hour_*`, `410_DE_hour_*`, `*_index_hour.json` | weeks covering 2024-06-12 and the windows 2024-03-28..04-03, 2024-10-24..10-30 | smard.de/app/chart_data | 2026-07-25 |
| SMARD prices (quarter-hour) | `4169_DE_quarterhour_*`, `4169_DE_index_quarterhour.json` | DST Sundays 2024-03-31, 2024-10-27 | smard.de/app/chart_data | (original M1) |
| Open-Meteo archive (actuals) | `open_meteo_archive_2024-0(3\|4)-*`, `open_meteo_archive_2024-10-*` | window days 2024-03-28..04-03, 2024-10-24..10-30 (one file per Berlin-day UTC window) | archive-api.open-meteo.com | 2026-07-25 |
| Home Assistant history | `home_assistant_<dataset>.json` | 2024-06-15 | a live Home Assistant instance, hand-sanitised | (original M3; `indoor_temperature` added M10) |

Home Assistant fixtures are **not** served by the offline transport — they are loaded directly by
`tests/connectors/test_home_assistant.py` through its own `MockTransport`, because the real
connector never runs in CI or in the demo. The filename carries the `Dataset` value, which is what
`scripts/record_home_assistant_fixtures.py <dataset> <entity>` writes; the two fixtures exercise
the connector's three conversion paths (power integration, percent level, and the M10 sampled-in-
own-units path that indoor temperature must not share with SoC).

## Curated fixtures (do not re-record — carry an injected gap)

These retain a single hand-injected `null` so the "NaN over fabrication" tests
(`test_archive_preserves_null_values_not_fabricated`, `test_forecast_returns_all_variables…`)
prove missing intervals are preserved as `None`, never zeroed or interpolated. Live re-recording
would erase the gap, so the recorders skip them.

| File | Injected gap | Used by |
|---|---|---|
| `open_meteo_archive_2024-06-11_2024-06-12.json` | 1× `null` in `shortwave_radiation` | archive null-preservation test; the normal-day reference in unit tests |
| `open_meteo_forecast.json` | 1× `null` in the horizon | forecast null-preservation test; the single offline forecast snapshot |

The recorded site is Berlin, rounded to 2 dp (`52.52, 13.40`) — the only coordinates anywhere in
the repo.
