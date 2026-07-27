"""Deterministic synthetic weather forecast vintages -- what the public demo backtests against.

M2 captures real Open-Meteo forecasts as immutable vintages, but the API serves only the *current*
issue, so vintages accrue forward and can never be backfilled. That leaves M7 with a hole it cannot
fill by ingesting harder: the seeded warehouse's telemetry is in 2024 and the one recorded fixture
vintage is issued in 2026, so nothing in the demo has both a forecast and the actual it should be
scored against.

This module fills it the way M3 filled the telemetry hole -- with a labelled synthetic producer
that lands in the identical raw-zone tables, so nothing downstream knows or cares which ran. A
vintage is a **pure function of ``(salt, generator version, issue day)`` and the already-ingested
M2 archive actuals**: take what actually happened, degrade it the way a forecast is wrong, and
stamp it with the instant a forecaster would have issued it.

**This producer can only ever run retrospectively.** It reconstructs an issue-time snapshot from
data that settles roughly five days *after* the instant it claims (ERA5; see ``ARCHIVE_LAG_DAYS``),
so it is the exact mirror image of the Open-Meteo client, which can only ever run forward. Neither
can do the other's job. It is deliberately **not** scheduled in Dagster -- a live daily run would be
claiming to forecast from data that does not exist yet.

The three M3 determinism rules carry over unchanged, and for the same reason -- this is ingestion,
so it is held to the raw zone's bit-reproducible content-hash contract, not to the weaker
value-stability the modelling layer settles for:

* **Seed recipe** -- ``sha256(f"{salt}:forecast:{GENERATOR_VERSION}:{issue_day}")``, via M3's
  :func:`~energy_platform.connectors.synthetic.seed_for`.
* **Single emission boundary** -- physics in full precision, :func:`~...synthetic.emit` quantises
  last.
* **stdlib only** -- ``random.Random`` and ``math``, no numpy, so float reprs and therefore hashes
  are identical across machines. ``tests/test_import_containment.py`` enforces this.

How the error is modelled, and why each piece is there:

* **A per-vintage regime offset.** One draw per issue day biases the whole horizon in the same
  direction. Real forecast error is autocorrelated -- a forecaster who is too sunny at 09:00 is
  usually still too sunny at 15:00 -- and independent per-hour noise would instead be averaged
  away by any model with enough data, making a residual correction look far better than it should.
* **Amplitude growing with lead time.** A target 40 hours out is forecast worse than one 4 hours
  out. This is what makes horizon-hour a meaningful axis in ``mart_forecast_eval`` rather than
  decoration.
* **One shared factor across all three irradiance components.** GHI, direct and diffuse are scaled
  together, so whatever relationship the archive had between them survives -- in particular
  ``ghi == direct + diffuse``, which M7's PV model relies on to recover DNI exactly rather than
  guessing it with a decomposition model. Perturbing them independently would quietly break that
  identity and make the physical model wrong for a reason nobody would find.
* **Missing stays missing.** An archive hour that is ``None`` yields a ``None`` forecast. A
  generator that invented a value where the truth has a hole would be fabricating exactly the thing
  the platform promises not to.

The RNG draws a fixed number of values per target hour whether or not that hour's actual is
present, so the stream stays aligned to the hour grid -- the same discipline as M3's generator.

**Changing this generator on an existing database.** Vintages are keyed on content, and
``issue_time`` is pinned to a fixed local hour rather than a wall clock, so a regenerated vintage
appends a second row sharing the ``(source, issue_date, issue_time, target_ts_utc)`` grain that
``stg_weather_forecast`` asserts is unique -- and the raw zone is append-only, so nothing removes
it. Bump :data:`GENERATOR_VERSION` *and* reset the vintages (``just forecast-reset``, or
``just demo-down`` for the whole volume). This is the honest cost of an append-only zone meeting a
regenerable producer; the alternative -- letting the generator mutate rows in place -- would break a
contract that is load-bearing everywhere else.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from typing import Final

from energy_platform.config import SyntheticConfig
from energy_platform.connectors.open_meteo import WEATHER_VARIABLES
from energy_platform.connectors.synthetic import (
    BERLIN,
    SOURCE,
    SOURCE_TZ,
    WEATHER_SOURCE,
    WeatherReader,
    emit,
    seed_for,
)
from energy_platform.connectors.types import Dataset, Point, RawForecast, Resolution

# Bump when the perturbation model changes: it is folded into the seed, so every emitted value and
# therefore every content hash moves with it. See the module docstring on resetting vintages.
GENERATOR_VERSION: Final = "v1"

# A forecaster issuing the evening before the target day. This is the instant M7's vintage rule
# selects on ("the last vintage issued strictly before the target day begins"), and it matches the
# information set a household planning tomorrow's battery schedule actually has.
ISSUE_HOUR_LOCAL: Final = 18

# 48 hours reaches the end of the day after the issue day, which is all the day-ahead horizon M8
# needs -- and, more to the point, all that a vintage issued at 18:00 must cover to describe the
# whole of the following Berlin day, including a 25-hour one.
HORIZON_HOURS: Final = 48

_HOUR_MS: Final = 3_600_000

# Error model. The regime terms are drawn once per vintage (correlated across the horizon); the
# noise terms once per target hour, with an amplitude that grows linearly with lead time.
_IRRADIANCE_REGIME_AMPLITUDE: Final = 0.18  # +/-18% systematic, per vintage
_IRRADIANCE_NOISE_BASE: Final = 0.05  # +/-5% at zero lead
_IRRADIANCE_NOISE_PER_HOUR: Final = 0.004  # ...growing to ~+/-24% at 47 h
_TEMP_REGIME_AMPLITUDE_C: Final = 1.5
_TEMP_NOISE_BASE_C: Final = 0.4
_TEMP_NOISE_PER_HOUR_C: Final = 0.03
_CLOUD_NOISE_BASE_PCT: Final = 4.0
_CLOUD_NOISE_PER_HOUR_PCT: Final = 0.25
_WIND_NOISE_BASE: Final = 0.08
_WIND_NOISE_PER_HOUR: Final = 0.004

# The three irradiance components move together under one factor; see the module docstring.
_IRRADIANCE_VARIABLES: Final[frozenset[Dataset]] = frozenset(
    {Dataset.SHORTWAVE_RADIATION, Dataset.DIRECT_RADIATION, Dataset.DIFFUSE_RADIATION}
)


class SyntheticForecastError(RuntimeError):
    """Raised when a synthetic vintage cannot be generated (e.g. an unsupported resolution)."""


def issue_time_for(issue_day: date) -> datetime:
    """The UTC instant a vintage for ``issue_day`` claims to have been issued at.

    Pinned to :data:`ISSUE_HOUR_LOCAL` Europe/Berlin, so it lands at a different UTC hour either
    side of a DST switch -- which is correct: the forecaster's evening does not move.
    """
    local = datetime.combine(issue_day, time(hour=ISSUE_HOUR_LOCAL), tzinfo=BERLIN)
    return local.astimezone(UTC)


def horizon_target_ms(issue_day: date, horizon_hours: int = HORIZON_HOURS) -> tuple[int, ...]:
    """The UTC hour instants a vintage covers: the issue instant, then each following hour."""
    start_ms = int(issue_time_for(issue_day).timestamp() * 1000)
    return tuple(start_ms + hour * _HOUR_MS for hour in range(horizon_hours))


def degrade_irradiance(actual_wm2: float, factor: float) -> float:
    """Scale an irradiance component by the vintage's error factor, floored at zero.

    Negative irradiance is unphysical, so a factor below -1 clips rather than inverting the sign.
    Note this makes the error distribution asymmetric near dawn and dusk, which is true of real
    forecasts too -- you cannot under-predict darkness.
    """
    return max(actual_wm2 * factor, 0.0)


class SyntheticForecastClient:
    """Generates one vintage per issue day from ingested archive actuals.

    Satisfies :class:`~energy_platform.connectors.base.ForecastConnector`, so it reaches the raw
    zone through the same :func:`~energy_platform.orchestration.ingest.ingest_forecast_vintage`
    the Open-Meteo client uses -- one content-hash contract and one horizon check, whichever
    producer ran.

    Constructed per issue day rather than taking one in ``fetch_forecast``, because the protocol's
    shape is set by the real client, which has no say in *when* it is forecasting: it can only
    return the current issue.
    """

    source: str = SOURCE

    def __init__(
        self,
        reader: WeatherReader,
        *,
        issue_day: date,
        synthetic: SyntheticConfig,
        horizon_hours: int = HORIZON_HOURS,
        weather_source: str = WEATHER_SOURCE,
    ) -> None:
        self._reader = reader
        self._issue_day = issue_day
        self._synthetic = synthetic
        self._horizon_hours = horizon_hours
        self._weather_source = weather_source

    @property
    def forecast_days(self) -> int:
        """Whole days the horizon reaches past the issue day -- the label stored on the vintage.

        Rounded up from the issue *instant*, so a 48-hour horizon from 18:00 reaches into the
        second following day and is labelled 2, which is what
        ``_require_targets_within_horizon`` checks the payload against.
        """
        return math.ceil(self._horizon_hours / 24)

    def fetch_forecast(
        self,
        site_id: str,
        resolution: Resolution = Resolution.HOUR,
        variables: Sequence[Dataset] = WEATHER_VARIABLES,
    ) -> RawForecast:
        if resolution is not Resolution.HOUR:
            raise SyntheticForecastError(
                f"synthetic forecasts are hourly; got resolution {resolution.value!r}"
            )

        targets = horizon_target_ms(self._issue_day, self._horizon_hours)
        actuals = self._read_actuals(site_id, variables, targets)
        rng = random.Random(
            seed_for(f"{self._synthetic.salt}:forecast:{GENERATOR_VERSION}", self._issue_day)
        )

        # One draw per vintage: the systematic direction this forecast is wrong in.
        irradiance_regime = 1.0 + rng.uniform(
            -_IRRADIANCE_REGIME_AMPLITUDE, _IRRADIANCE_REGIME_AMPLITUDE
        )
        temp_regime = rng.uniform(-_TEMP_REGIME_AMPLITUDE_C, _TEMP_REGIME_AMPLITUDE_C)

        series: dict[Dataset, list[Point]] = {variable: [] for variable in variables}
        for lead_hours, target_ms in enumerate(targets):
            # Draw every hour's noise unconditionally, before consulting the actuals, so a missing
            # hour does not shift the RNG stream for every hour after it.
            irradiance_factor = irradiance_regime + rng.uniform(
                *_noise_bounds(_IRRADIANCE_NOISE_BASE, _IRRADIANCE_NOISE_PER_HOUR, lead_hours)
            )
            temp_offset = temp_regime + rng.uniform(
                *_noise_bounds(_TEMP_NOISE_BASE_C, _TEMP_NOISE_PER_HOUR_C, lead_hours)
            )
            cloud_offset = rng.uniform(
                *_noise_bounds(_CLOUD_NOISE_BASE_PCT, _CLOUD_NOISE_PER_HOUR_PCT, lead_hours)
            )
            wind_factor = 1.0 + rng.uniform(
                *_noise_bounds(_WIND_NOISE_BASE, _WIND_NOISE_PER_HOUR, lead_hours)
            )

            for variable in variables:
                actual = actuals[variable].get(target_ms)
                series[variable].append(
                    (
                        target_ms,
                        emit(
                            _degrade(
                                variable,
                                actual,
                                irradiance_factor=irradiance_factor,
                                temp_offset=temp_offset,
                                cloud_offset=cloud_offset,
                                wind_factor=wind_factor,
                            )
                        ),
                    )
                )

        return RawForecast(
            source=SOURCE,
            region=site_id,
            resolution=resolution,
            source_tz=SOURCE_TZ,
            # Provenance, not a URL: this vintage was computed, not fetched. Spelled as a URI so
            # the column keeps one shape across producers.
            source_urls=(
                f"synthetic:forecast/{GENERATOR_VERSION}"
                f"?issue={self._issue_day.isoformat()}&horizon_hours={self._horizon_hours}",
            ),
            series={variable: tuple(points) for variable, points in series.items()},
        )

    def _read_actuals(
        self, site_id: str, variables: Sequence[Dataset], targets: tuple[int, ...]
    ) -> dict[Dataset, dict[int, float | None]]:
        """The archive truth this vintage degrades, indexed by target instant.

        Read as one span per variable rather than per day: the horizon straddles up to three Berlin
        days, and an hour whose archive row has not been ingested is simply absent here and becomes
        a ``None`` forecast value -- the same treatment as an ingested null.
        """
        start_ms, end_ms = targets[0], targets[-1] + _HOUR_MS
        return {
            variable: {
                ts: value
                for ts, value in self._reader.read_current_series(
                    self._weather_source,
                    variable.value,
                    site_id,
                    Resolution.HOUR.value,
                    start_ms,
                    end_ms,
                )
            }
            for variable in variables
        }


def _noise_bounds(base: float, per_hour: float, lead_hours: int) -> tuple[float, float]:
    """Symmetric ``(low, high)`` noise bounds widening linearly with lead time."""
    amplitude = base + per_hour * lead_hours
    return -amplitude, amplitude


def _degrade(
    variable: Dataset,
    actual: float | None,
    *,
    irradiance_factor: float,
    temp_offset: float,
    cloud_offset: float,
    wind_factor: float,
) -> float | None:
    """Apply this hour's error to one variable. ``None`` in, ``None`` out -- never fabricated."""
    if actual is None:
        return None
    if variable in _IRRADIANCE_VARIABLES:
        return degrade_irradiance(actual, irradiance_factor)
    if variable is Dataset.TEMPERATURE_2M:
        return actual + temp_offset
    if variable is Dataset.CLOUD_COVER:
        return min(max(actual + cloud_offset, 0.0), 100.0)
    if variable is Dataset.WIND_SPEED_10M:
        return max(actual * wind_factor, 0.0)
    raise SyntheticForecastError(f"no error model for weather variable {variable.value!r}")
