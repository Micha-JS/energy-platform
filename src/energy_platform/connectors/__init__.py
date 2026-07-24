"""External data connectors: SMARD/ENTSO-E, Open-Meteo, OpenEMS/Home Assistant.

M1 ships the SMARD market-data connector behind the source-agnostic
:class:`~energy_platform.connectors.base.MarketDataConnector` protocol.
"""

from energy_platform.connectors.base import MarketDataConnector
from energy_platform.connectors.smard import SmardClient, SmardError
from energy_platform.connectors.types import (
    Dataset,
    Point,
    RawSeries,
    Resolution,
    UtcWindow,
)

__all__ = [
    "Dataset",
    "MarketDataConnector",
    "Point",
    "RawSeries",
    "Resolution",
    "SmardClient",
    "SmardError",
    "UtcWindow",
]
