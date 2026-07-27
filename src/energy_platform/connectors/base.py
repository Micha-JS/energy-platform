"""The connector protocols every data source implements."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from energy_platform.connectors.types import (
    Dataset,
    RawForecast,
    RawSeries,
    Resolution,
    UtcWindow,
)


@runtime_checkable
class MarketDataConnector(Protocol):
    """A source of time-series market data.

    Implementations fetch whatever native files/pages cover ``window`` and return the
    series sliced to it. They must not fabricate, interpolate, or drop values -- a value
    the source reports as missing is returned as ``None``.
    """

    source: str

    def fetch_window(
        self,
        dataset: Dataset,
        region: str,
        resolution: Resolution,
        window: UtcWindow,
    ) -> RawSeries: ...


@runtime_checkable
class ForecastConnector(Protocol):
    """A source of forecast vintages -- every variable's horizon, captured as one snapshot.

    Distinct from :class:`MarketDataConnector` because the shape of the truth is different: a
    market series is one settled variable over a requested window, a vintage is *all* variables
    over a horizon the source chooses, valid only as of the instant it was issued. They land in
    different raw-zone tables through different ingestion functions.

    Two implementations, with opposite relationships to time.
    :class:`~energy_platform.connectors.open_meteo.OpenMeteoForecastClient` can only ever return
    the *current* issue, so real vintages accrue forward and can never be backfilled. The synthetic
    generator can only ever run *retrospectively*, because it reconstructs a plausible forecast
    from archive actuals that settle days after the issue instant it claims. Neither can do the
    other's job, which is why both exist.

    ``forecast_days`` is declared as a property rather than an attribute deliberately: protocol
    *variables* are read-write, so an implementation exposing it as a read-only ``@property``
    (as the Open-Meteo client does) would fail to satisfy a plain attribute declaration under
    mypy strict. Note also that structural matching compares parameter *names* -- an implementation
    must spell ``site_id``, ``resolution`` and ``variables`` exactly.
    """

    source: str

    @property
    def forecast_days(self) -> int: ...

    def fetch_forecast(
        self,
        site_id: str,
        resolution: Resolution = ...,
        variables: Sequence[Dataset] = ...,
    ) -> RawForecast: ...
