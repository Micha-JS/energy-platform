"""Publishing the day's dispatch plan to a broker a Home Assistant instance can read.

The first thing this platform *emits*. Everything before M10 pulls data in, transforms it and
serves it to a human; this hands a machine-readable plan to the house the data came from.

Three properties are load-bearing and each has a module:

* :mod:`~energy_platform.publishing.contract` -- the payload is **versioned** and says, in a field
  rather than only in prose, that it is a recommendation. Nothing in this repo actuates anything.
* :mod:`~energy_platform.publishing.reader` -- the plan is **read**, never recomputed. M8 already
  decided it and the warehouse already holds it.
* :mod:`~energy_platform.publishing.client` -- the broker is injected, disabled by default, and
  configured only from the environment, exactly as the real Home Assistant connector is.

Nothing here is imported by :mod:`energy_platform.definitions` at module scope; the MQTT client is
loaded lazily so a Dagster code location or a ``backfill`` run does not pay for it.
"""

from __future__ import annotations
