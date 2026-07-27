"""Tests for the synthetic forecast vintage generator.

Nothing here touches Postgres or the network: a vintage is a pure function of the salt, the
generator version, the issue day, and the archive actuals handed to it, which is exactly what makes
it property-testable and what makes re-ingestion a content-hash no-op.

The invariants pinned here are the ones the raw zone's contract rests on (determinism, one
quantisation boundary, missing-stays-missing, RNG stream alignment) plus the two the *modelling*
layer rests on: the irradiance components must stay in proportion so ``ghi == direct + diffuse``
survives, and error must grow with lead time or ``horizon_hour`` is a decorative axis in the eval
mart.
"""

from __future__ import annotations

import statistics
from datetime import UTC, date, datetime

import pytest

from energy_platform.config import SyntheticConfig
from energy_platform.connectors.base import ForecastConnector
from energy_platform.connectors.synthetic_forecast import (
    HORIZON_HOURS,
    SyntheticForecastClient,
    SyntheticForecastError,
    horizon_target_ms,
    issue_time_for,
)
from energy_platform.connectors.types import Dataset, Point, Resolution
from energy_platform.orchestration.ingest import _require_targets_within_horizon

SITE = "home"
ISSUE_DAY = date(2024, 6, 11)
SYNTHETIC = SyntheticConfig()

# A plausible June day: irradiance up over the middle of the day, mild temperatures. Values are
# arbitrary but the direct/diffuse split sums to the GHI, as Open-Meteo's do.
_DIURNAL_GHI = (
    0.0, 0.0, 0.0, 12.0, 68.0, 160.0, 290.0, 430.0, 560.0, 660.0, 720.0, 745.0,
    740.0, 700.0, 620.0, 500.0, 360.0, 220.0, 100.0, 30.0, 0.0, 0.0, 0.0, 0.0,
)  # fmt: skip


class StubWeatherReader:
    """Serves a fixed archive day, repeated, with optional holes -- the M3 stub pattern."""

    def __init__(self, missing_ts: frozenset[int] = frozenset()) -> None:
        self.missing_ts = missing_ts

    def read_current_series(
        self,
        source: str,
        dataset: str,
        region: str,
        resolution: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[Point, ...]:
        points: list[Point] = []
        ts = start_ms
        while ts < end_ms:
            if ts in self.missing_ts:
                points.append((ts, None))
            else:
                points.append((ts, _archive_value(dataset, ts)))
            ts += 3_600_000
        return tuple(points)


def _archive_value(dataset: str, ts_ms: int) -> float:
    hour = datetime.fromtimestamp(ts_ms / 1000, UTC).hour
    ghi = _DIURNAL_GHI[hour]
    if dataset == Dataset.SHORTWAVE_RADIATION.value:
        return ghi
    if dataset == Dataset.DIRECT_RADIATION.value:
        return ghi * 0.7
    if dataset == Dataset.DIFFUSE_RADIATION.value:
        return ghi * 0.3
    if dataset == Dataset.TEMPERATURE_2M.value:
        return 12.0 + ghi / 100.0
    if dataset == Dataset.CLOUD_COVER.value:
        return 40.0
    return 3.0  # wind speed


def _client(
    issue_day: date = ISSUE_DAY,
    *,
    reader: StubWeatherReader | None = None,
    synthetic: SyntheticConfig = SYNTHETIC,
) -> SyntheticForecastClient:
    return SyntheticForecastClient(
        reader or StubWeatherReader(), issue_day=issue_day, synthetic=synthetic
    )


# -- The issue instant -------------------------------------------------------------------


def test_the_vintage_is_issued_at_18_00_local() -> None:
    issued = issue_time_for(date(2024, 6, 11))
    assert issued == datetime(2024, 6, 11, 16, 0, tzinfo=UTC)  # CEST, UTC+2


def test_the_forecasters_evening_does_not_move_across_dst() -> None:
    """18:00 local is a different UTC hour either side of the switch, which is the point.

    Pinning the issue to a UTC hour instead would drift the information set by an hour twice a
    year -- silently, and only on the two days the rest of the platform is most careful about.
    """
    assert issue_time_for(date(2024, 3, 30)).hour == 17  # CET, UTC+1
    assert issue_time_for(date(2024, 3, 31)).hour == 16  # CEST, UTC+2


def test_the_issue_instant_precedes_the_day_the_vintage_forecasts() -> None:
    """The vintage rule selects on 'strictly before the target day begins'; 18:00 the evening
    before must satisfy it, or the demo has no selectable vintage at all."""
    issued = issue_time_for(ISSUE_DAY)
    target_day_start = datetime(2024, 6, 12, 0, 0, tzinfo=UTC)
    assert issued < target_day_start


def test_the_horizon_covers_the_whole_following_berlin_day() -> None:
    targets = horizon_target_ms(ISSUE_DAY)
    assert len(targets) == HORIZON_HOURS
    covered = {datetime.fromtimestamp(ts / 1000, UTC).date() for ts in targets}
    assert date(2024, 6, 12) in covered


def test_the_payload_stays_inside_the_horizon_it_claims() -> None:
    """The label stored on the vintage must describe the data, or nothing downstream can tell."""
    forecast = _client().fetch_forecast(SITE)
    _require_targets_within_horizon(forecast.series, ISSUE_DAY, _client().forecast_days)


# -- Determinism (the raw zone's contract) -----------------------------------------------


def test_a_vintage_is_a_pure_function_of_its_inputs() -> None:
    assert _client().fetch_forecast(SITE).series == _client().fetch_forecast(SITE).series


def test_changing_the_salt_changes_every_value() -> None:
    other = SyntheticConfig(salt="a-different-salt")
    assert (
        _client().fetch_forecast(SITE).series
        != _client(synthetic=other).fetch_forecast(SITE).series
    )


def test_different_issue_days_are_independently_seeded() -> None:
    a = _client(date(2024, 6, 11)).fetch_forecast(SITE)
    b = _client(date(2024, 6, 12)).fetch_forecast(SITE)
    assert [v for _, v in a.series[Dataset.TEMPERATURE_2M]] != [
        v for _, v in b.series[Dataset.TEMPERATURE_2M]
    ]


def test_values_are_quantised_at_one_emission_boundary() -> None:
    for points in _client().fetch_forecast(SITE).series.values():
        for _, value in points:
            assert value is None or value == round(value, 3)


# -- NaN over fabrication ----------------------------------------------------------------


def test_a_missing_actual_yields_a_missing_forecast() -> None:
    hole = horizon_target_ms(ISSUE_DAY)[5]
    forecast = _client(reader=StubWeatherReader(frozenset({hole}))).fetch_forecast(SITE)
    for variable, points in forecast.series.items():
        assert dict(points)[hole] is None, f"{variable.value} fabricated a value over a hole"


def test_a_hole_does_not_shift_the_rng_stream_for_later_hours() -> None:
    """Every hour draws its noise before consulting the actuals, so a gap is local.

    Without that discipline one missing hour would change every value after it, and a re-ingest
    after a late-arriving archive row would rewrite a whole vintage instead of one hour.
    """
    targets = horizon_target_ms(ISSUE_DAY)
    hole = targets[5]
    whole = dict(_client().fetch_forecast(SITE).series[Dataset.TEMPERATURE_2M])
    holed = dict(
        _client(reader=StubWeatherReader(frozenset({hole})))
        .fetch_forecast(SITE)
        .series[Dataset.TEMPERATURE_2M]
    )
    for ts in targets:
        if ts == hole:
            assert holed[ts] is None
        else:
            assert holed[ts] == whole[ts], "a gap perturbed an hour it should not have touched"


# -- The physics the PV model depends on -------------------------------------------------


def test_irradiance_is_never_negative() -> None:
    forecast = _client().fetch_forecast(SITE)
    for variable in (
        Dataset.SHORTWAVE_RADIATION,
        Dataset.DIRECT_RADIATION,
        Dataset.DIFFUSE_RADIATION,
    ):
        assert all(value is None or value >= 0.0 for _, value in forecast.series[variable])


def test_the_irradiance_components_stay_in_proportion() -> None:
    """One shared factor, so ``ghi == direct + diffuse`` survives the perturbation.

    M7's PV model recovers DNI as ``direct / cos(zenith)`` rather than guessing it with a
    decomposition model, which is only sound while the components remain mutually consistent.
    Perturbing them independently would break that quietly and make the physical model wrong for
    a reason nobody would think to look for.
    """
    forecast = _client().fetch_forecast(SITE)
    ghi = dict(forecast.series[Dataset.SHORTWAVE_RADIATION])
    direct = dict(forecast.series[Dataset.DIRECT_RADIATION])
    diffuse = dict(forecast.series[Dataset.DIFFUSE_RADIATION])
    checked = 0
    for ts, total in ghi.items():
        if not total:  # skip night, where the identity is 0 == 0 and proves nothing
            continue
        beam, sky = direct[ts], diffuse[ts]
        assert beam is not None and sky is not None
        assert beam + sky == pytest.approx(total, abs=0.005), (
            "the components drifted apart -- DNI recovery is unsound"
        )
        checked += 1
    assert checked > 0, "no daylight hours in the fixture; the assertion proved nothing"


def test_cloud_cover_stays_a_percentage() -> None:
    forecast = _client().fetch_forecast(SITE)
    values = [v for _, v in forecast.series[Dataset.CLOUD_COVER] if v is not None]
    assert values and all(0.0 <= value <= 100.0 for value in values)


# -- The error model ---------------------------------------------------------------------


def test_forecast_error_grows_with_lead_time() -> None:
    """Otherwise ``horizon_hour`` is a decorative axis in mart_forecast_eval.

    Measured across many issue days, because a single vintage's regime offset dominates its own
    horizon -- which is the intended behaviour, and precisely why one day cannot show the trend.
    """
    early: list[float] = []
    late: list[float] = []
    for offset in range(60):
        day = date.fromordinal(date(2024, 4, 1).toordinal() + offset)
        forecast = _client(day).fetch_forecast(SITE)
        for lead, (ts, value) in enumerate(forecast.series[Dataset.TEMPERATURE_2M]):
            if value is None:
                continue
            error = abs(value - _archive_value(Dataset.TEMPERATURE_2M.value, ts))
            if lead < 6:
                early.append(error)
            elif lead >= 40:
                late.append(error)

    assert early and late
    assert statistics.mean(late) > statistics.mean(early), (
        f"mean |error| did not grow with lead time: {statistics.mean(early):.3f} at 0-5 h vs "
        f"{statistics.mean(late):.3f} at 40+ h"
    )


def test_forecast_error_is_correlated_within_a_vintage_not_white() -> None:
    """A vintage is systematically too sunny or too dull, not independently wrong each hour.

    Independent per-hour noise would be averaged away by any model with enough data, making a
    residual correction look far better than a real one ever does. The discriminator is
    *between-vintage* spread of the mean error: averaging 48 independent draws of amplitude ~0.15
    would leave a per-vintage mean within a couple of percent of 1.0 every time, so the means would
    cluster tightly. A shared regime term per vintage instead spreads them across roughly the
    regime's own range.

    Note this is deliberately not asserted per-vintage: the noise amplitude grows to ~+/-24% by the
    end of the horizon and legitimately overwhelms a small regime draw, so individual late hours can
    and should fall on either side.
    """
    vintage_means: list[float] = []
    for offset in range(40):
        day = date.fromordinal(date(2024, 4, 1).toordinal() + offset)
        ghi = dict(_client(day).fetch_forecast(SITE).series[Dataset.SHORTWAVE_RADIATION])
        ratios = [
            value / truth
            for ts, value in ghi.items()
            if value is not None
            and (truth := _archive_value(Dataset.SHORTWAVE_RADIATION.value, ts)) > 0
        ]
        assert ratios
        vintage_means.append(statistics.mean(ratios))

    spread = statistics.stdev(vintage_means)
    assert spread > 0.05, (
        f"per-vintage mean error ratios cluster too tightly (sd={spread:.4f}) -- the regime offset "
        "is not doing its job and the error is effectively white"
    )


# -- Protocol conformance ----------------------------------------------------------------


def test_it_satisfies_the_forecast_connector_protocol() -> None:
    """Structural, so it reaches the raw zone through the same ingestion core as Open-Meteo."""
    client: ForecastConnector = _client()  # mypy checks this assignment; isinstance checks runtime
    assert isinstance(client, ForecastConnector)
    assert client.source == "synthetic"
    assert client.forecast_days == 2


def test_sub_hourly_resolution_is_refused() -> None:
    with pytest.raises(SyntheticForecastError, match="hourly"):
        _client().fetch_forecast(SITE, Resolution.QUARTERHOUR)
