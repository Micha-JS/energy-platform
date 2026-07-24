"""The connector protocol every market-data source implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from energy_platform.connectors.types import Dataset, RawSeries, Resolution, UtcWindow


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
