"""Physical PV models: M3's flat-plate toy, and a pvlib plane-of-array chain.

Both map weather to AC energy. They disagree, and the disagreement is the interesting part.

**The toy model is the oracle on synthetic data.** M3 generates synthetic PV as
``dc_kwp * (ghi/1000) * PR * temp_derate`` -- irradiance on a *horizontal* surface, no panel tilt
anywhere (``connectors/synthetic.py``). So on seeded windows :func:`toy_ac_kwh` is not a competing
model, it is the data-generating process, and it wins its own evaluation by tautology. That is why
``mart_forecast_eval`` carries a ``role`` column marking it, rather than a README footnote asking
readers to remember.

**The pvlib model is the one that will be right on the real array.** A real 8.8 kWp Fenecon
installation sits on a pitched roof; plane-of-array irradiance is what its panels actually receive,
and the flat-plate approximation loses exactly the geometry that matters. So M7 ships a falsifiable
prediction: against real telemetry the ranking inverts, and the physical model overtakes the toy.
Until that telemetry exists, the honest statement is that the seeded comparison cannot settle it.

Two things this chain does *not* have to guess, both because M2 ingested more than it needed to:

* **DNI is recovered exactly, not decomposed.** Open-Meteo's ``direct_radiation`` is direct
  *horizontal* irradiance -- ``DNI * cos(zenith)`` -- so ``DNI = direct_radiation / cos(zenith)``
  with a low-sun cutoff. The usual hybrid has to estimate DNI from GHI with Erbs or DISC and inherit
  that model's error; this one does not. It is also why the synthetic vintage generator scales all
  three irradiance components by one shared factor: independent perturbation would break the
  identity this relies on.
* **Cell temperature uses measured wind.** ``sapm_cell`` takes wind speed, and M2 has been ingesting
  10 m wind since before anything needed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd
import pvlib
from numpy.typing import NDArray
from pvlib.temperature import TEMPERATURE_MODEL_PARAMETERS

from energy_platform.config import PvSystemConfig
from energy_platform.connectors.synthetic import pv_production_kwh

# Below this solar elevation the ``1/cos(zenith)`` in the DNI recovery explodes: at 85 degrees the
# divisor is already 0.087, and the horizontal component it divides is rounding noise. Past it the
# beam is treated as zero and the (correctly large) diffuse fraction carries the hour. This is the
# same cutoff pvlib's own decomposition models use, for the same reason.
_ZENITH_CUTOFF_DEG: Final = 85.0

# Open-rack glass-glass: the mounting a roof array is closest to among the SAPM parameter sets, and
# the conservative choice -- close-roof mounting runs hotter and would derate further.
_TEMPERATURE_MODEL: Final = TEMPERATURE_MODEL_PARAMETERS["sapm"]["open_rack_glass_glass"]

# PVWatts' nominal inverter efficiency. `pvlib.inverter.pvwatts` takes `pdc0` as the DC input limit,
# NOT the AC ceiling, and delivers at most `pdc0 * eta_inv_nom` -- so clipping at the nameplate AC
# cap means dividing by this first. Passing the AC cap directly caps ~4% low, silently.
_ETA_INV_NOM: Final = 0.96

MODEL_TOY: Final = "toy_flat_plate"
MODEL_PVLIB: Final = "pvlib_poa"


@dataclass(frozen=True, slots=True)
class SolarGeometry:
    """Where the sun is, per target instant. A fact about orbital mechanics, not about data.

    Knowable arbitrarily far ahead, which is why the features derived from it carry no availability
    instant and cannot leak.
    """

    zenith_deg: NDArray[np.float64]
    azimuth_deg: NDArray[np.float64]
    dni_extra_wm2: NDArray[np.float64]

    @property
    def cos_zenith(self) -> NDArray[np.float64]:
        return np.cos(np.radians(self.zenith_deg))


def solar_geometry(
    ts_utc_ms: NDArray[np.int64], latitude: float, longitude: float
) -> SolarGeometry:
    """Solar position and extraterrestrial irradiance for a series of UTC instants.

    pvlib requires a localized ``pandas.DatetimeIndex`` here -- there is no numpy-array path -- and
    that is the single reason pandas is in the dependency tree at all. Everything downstream of
    this function is numpy.
    """
    times = pd.to_datetime(ts_utc_ms, unit="ms", utc=True)
    position = pvlib.solarposition.get_solarposition(times, latitude, longitude)
    return SolarGeometry(
        # Apparent (refraction-corrected) zenith: what the beam actually does near the horizon.
        zenith_deg=position["apparent_zenith"].to_numpy(dtype=float),
        azimuth_deg=position["azimuth"].to_numpy(dtype=float),
        dni_extra_wm2=np.asarray(pvlib.irradiance.get_extra_radiation(times), dtype=float),
    )


def recover_dni(
    direct_horizontal_wm2: NDArray[np.float64], geometry: SolarGeometry
) -> NDArray[np.float64]:
    """Direct *normal* irradiance from direct *horizontal*, exactly rather than by decomposition."""
    cos_zenith = geometry.cos_zenith
    usable = (geometry.zenith_deg < _ZENITH_CUTOFF_DEG) & (cos_zenith > 0)
    return np.where(usable, direct_horizontal_wm2 / np.where(usable, cos_zenith, 1.0), 0.0)


def plane_of_array_wm2(
    ghi_wm2: NDArray[np.float64],
    direct_horizontal_wm2: NDArray[np.float64],
    diffuse_wm2: NDArray[np.float64],
    geometry: SolarGeometry,
    pv: PvSystemConfig,
) -> NDArray[np.float64]:
    """Total irradiance on the tilted plane, via Hay-Davies.

    Hay-Davies rather than isotropic because it accounts for circumsolar brightening, which is a
    large part of what a tilted plane gains over a horizontal one; it needs ``dni_extra``, and
    omitting that argument raises rather than silently falling back.
    """
    total = pvlib.irradiance.get_total_irradiance(
        surface_tilt=pv.tilt_deg,
        surface_azimuth=pv.azimuth_deg,
        solar_zenith=geometry.zenith_deg,
        solar_azimuth=geometry.azimuth_deg,
        dni=recover_dni(direct_horizontal_wm2, geometry),
        ghi=ghi_wm2,
        dhi=diffuse_wm2,
        dni_extra=geometry.dni_extra_wm2,
        model="haydavies",
    )
    return np.asarray(total["poa_global"], dtype=float)


def pvlib_ac_kwh(
    ghi_wm2: NDArray[np.float64],
    direct_horizontal_wm2: NDArray[np.float64],
    diffuse_wm2: NDArray[np.float64],
    temperature_c: NDArray[np.float64],
    wind_speed_ms: NDArray[np.float64],
    geometry: SolarGeometry,
    pv: PvSystemConfig,
) -> NDArray[np.float64]:
    """The full plane-of-array chain, in kWh per hour at the AC coupling point.

    Reuses M3's ``PvSystemConfig`` unchanged so the two models are parameterised identically and the
    comparison is about geometry rather than about calibration: ``dc_kwp`` is ``pdc0``,
    ``temp_coeff_per_c`` is ``gamma_pdc``, ``performance_ratio`` folds in soiling and wiring losses,
    ``ac_cap_kw`` is the inverter ceiling.

    ``NaN`` propagates through every step, so a missing weather hour yields a missing prediction
    rather than a fabricated zero.
    """
    poa = plane_of_array_wm2(ghi_wm2, direct_horizontal_wm2, diffuse_wm2, geometry, pv)
    cell_temp = pvlib.temperature.sapm_cell(
        poa_global=poa,
        temp_air=temperature_c,
        wind_speed=wind_speed_ms,
        **_TEMPERATURE_MODEL,
    )
    dc_w = pvlib.pvsystem.pvwatts_dc(
        effective_irradiance=poa,
        temp_cell=cell_temp,
        pdc0=pv.dc_kwp * 1000.0,
        gamma_pdc=pv.temp_coeff_per_c,
    )
    ac_w = pvlib.inverter.pvwatts(
        pdc=np.asarray(dc_w, dtype=float) * pv.performance_ratio,
        pdc0=pv.ac_cap_kw * 1000.0 / _ETA_INV_NOM,
    )
    # One hour per step, so W and Wh coincide; negatives are the inverter's night-time tare, which
    # a household meter does not see as production.
    return np.clip(np.asarray(ac_w, dtype=float), 0.0, None) / 1000.0


def toy_ac_kwh(
    ghi_wm2: NDArray[np.float64], temperature_c: NDArray[np.float64], pv: PvSystemConfig
) -> NDArray[np.float64]:
    """M3's flat-plate model, vectorised over an hour series.

    Delegates to :func:`energy_platform.connectors.synthetic.pv_production_kwh` per hour rather than
    reimplementing it. That is the point: on synthetic windows this function must be *bit-identical*
    to the process that produced the labels, or the oracle row in the eval mart would be an
    approximation of the oracle and the whole framing would be a little bit false. A test asserts
    the equality; delegation makes it true by construction.
    """
    return np.array(
        [
            np.nan
            if not np.isfinite(ghi)
            else pv_production_kwh(float(ghi), None if not np.isfinite(temp) else float(temp), pv)
            for ghi, temp in zip(ghi_wm2, temperature_c, strict=True)
        ],
        dtype=float,
    )


def geometry_feature_values(geometry: SolarGeometry, index: int) -> dict[str, float]:
    """Solar-geometry features for one hour, for the gradient-boosted models.

    Handed to the models as ordinary features rather than only through the physical chain, so a
    model can learn a correction that depends on where the sun is -- which, on synthetic windows, is
    exactly how it discovers the transposition it needs to undo.
    """
    zenith = float(geometry.zenith_deg[index])
    return {
        "solar_zenith_deg": zenith,
        "solar_azimuth_deg": float(geometry.azimuth_deg[index]),
        "cos_zenith": float(np.cos(np.radians(zenith))),
        "dni_extra_wm2": float(geometry.dni_extra_wm2[index]),
    }
