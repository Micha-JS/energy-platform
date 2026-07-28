"""Runtime configuration for the energy platform.

Config is read from the environment with typed dataclasses -- no settings library,
matching the env-driven ``DAGSTER_POSTGRES_*`` convention already used by the Dagster
stack. The raw zone reuses the existing ``dagster`` Postgres database under a separate
``raw`` schema, so the dockerised stack and a host-run CLI both work with zero extra
configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote

# Public SMARD chart-data base URL (no API key required, CC BY 4.0).
DEFAULT_SMARD_BASE_URL = "https://www.smard.de/app/chart_data"
DEFAULT_RAW_SCHEMA = "raw"

# The three schemas of the warehouse, and the contract each one holds:
#   raw       -- append-only, content-hashed; re-ingestion is a verifiable no-op (M1)
#   analytics -- dbt's output; `analytics_marts` is the target schema plus the marts layer suffix
#   derived   -- Python-computed results dbt reads back as a source (M6's dispatch optimiser).
#                Replace-on-rerun, NOT append-with-hash: an optimal schedule is not unique, so a
#                re-solve returning a different one is correct rather than a revision to preserve.
DEFAULT_MARTS_SCHEMA = "analytics_marts"
DEFAULT_STAGING_SCHEMA = "analytics_staging"
DEFAULT_DERIVED_SCHEMA = "derived"

# Public Open-Meteo API base URLs (no API key required, CC BY 4.0).
DEFAULT_OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DEFAULT_OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_FORECAST_DAYS = 7

# The Open-Meteo archive is ERA5-backed and only finalises a few days after the fact, so nothing
# downstream of it can know an hour's value until this much later. Two consumers, deliberately one
# definition: the Dagster schedules target `today - ARCHIVE_LAG_DAYS` rather than chasing yesterday
# into empty partitions (energy_platform.orchestration.schedules), and M7's backtester treats it as
# the *observation lag* -- synthetic telemetry is generated from this weather, so an hour of
# synthetic telemetry is not knowable until the weather behind it has settled. A "same hour
# yesterday" persistence baseline is therefore itself lookahead on this platform, which is the sort
# of thing that stays true only while the two numbers cannot drift apart.
ARCHIVE_LAG_DAYS = 5
DEFAULT_TELEMETRY_LAG_HOURS = ARCHIVE_LAG_DAYS * 24

# The site's coordinates, rounded to ~2 decimal places (~1 km) so the public repo never
# reveals a precise address. These rounded literals are the ONLY coordinates anywhere in the
# repo -- there is no precise copy in a gitignored file to leak. Berlin city centre by
# default; override per deployment via ENERGY_SITE_LAT / ENERGY_SITE_LON.
DEFAULT_SITE_ID = "home"
DEFAULT_SITE_LATITUDE = 52.52
DEFAULT_SITE_LONGITUDE = 13.40

# Fenecon Home 10 system, published as ordinary config (only the *values* of real telemetry
# are private -- the system's nameplate parameters are not). These same PV and battery numbers
# parameterise both the M3 synthetic generator and, later, the M6 dispatch optimiser, so the
# "actual" and "optimal" sides of the savings comparison run on identical physics.
DEFAULT_PV_DC_KWP = 8.8
DEFAULT_PV_AC_CAP_KW = 8.0
DEFAULT_PV_PERFORMANCE_RATIO = 0.82
DEFAULT_PV_TEMP_COEFF_PER_C = -0.004
DEFAULT_BATTERY_CAPACITY_KWH = 14.0

# Panel orientation, added at M7 for the pvlib plane-of-array model. Committed as ordinary config
# for the same reason the nameplate ratings are: an array's tilt and bearing say nothing about where
# it is -- half the pitched roofs in Germany are within a few degrees of these -- while the site's
# coordinates stay rounded above. 35 degrees is a conventional German roof pitch; 180 is due south
# in pvlib's convention (0 = north, clockwise).
DEFAULT_PV_TILT_DEG = 35.0
DEFAULT_PV_AZIMUTH_DEG = 180.0

# The conditioned zone and its air conditioner, added at M10. Same rationale as the battery
# numbers above: M10's thermostat is the "actual behaviour" baseline that M11's thermal optimiser
# will be measured against, so both sides must read one physics config or the comparison is an
# artefact of two different houses.
#
# ONE zone, ONE unit, LINEAR dynamics -- deliberately. The generator's job is to be a *knowable*
# oracle, and every parameter added here is one M11 has to identify later. R is the envelope's
# thermal resistance (K per kW of heat flow), C its effective capacitance (kWh per K), so the
# free-running time constant is R*C = 48 h. The solar term converts global horizontal irradiance
# straight to a heat gain in kW, standing in for glazing area, orientation and shading in a single
# number rather than three unidentifiable ones.
DEFAULT_THERMAL_R_K_PER_KW = 6.0
DEFAULT_THERMAL_C_KWH_PER_K = 8.0
DEFAULT_THERMAL_SOLAR_GAIN_KW_PER_WM2 = 0.001
# Thermostat and unit. 24 degC with a 1 K deadband is a conservative German cooling setpoint; COP 3
# and 2 kW electrical are a mid-size split unit. Sized so the AC is a *visible* load on the seeded
# summer days -- a unit that never runs would leave M11 with nothing to optimise.
DEFAULT_THERMOSTAT_SETPOINT_C = 24.0
DEFAULT_THERMOSTAT_DEADBAND_K = 1.0
DEFAULT_AC_COP = 3.0
DEFAULT_AC_RATED_KW = 2.0
# The floor a German house's heating holds in the shoulder seasons. NOT an electrical load and
# never part of `ac_power`: heating here is gas or district heat, so it appears in the model only
# as a lower bound on the zone temperature. Without it a cooling-only model reports ~12 degC
# indoors across the seeded March and October windows, which would be a nonsense series for M11 to
# learn from. Heating as a *dispatchable* load is out of scope for M10 and M11 alike.
DEFAULT_HEATING_SETPOINT_C = 20.0

# Which rows of the tariff catalogue (dbt/seeds/tariffs.csv) are in force. Only *ids* live here
# -- the rates themselves live in the catalogue, which dbt seeds and the Python engine reads, so
# there is exactly one copy of every number. These ids must match the dbt vars of the same name.
DEFAULT_CONSUMPTION_TARIFF_ID = "static_2024"
DEFAULT_FEED_IN_TARIFF_ID = "eeg_2024"


def _env(*names: str, default: str) -> str:
    """Return the first *set* environment variable among ``names``, else ``default``.

    A variable set to the empty string counts as set and wins over the default -- an
    explicit ``ENERGY_PG_PASSWORD=`` (a valid empty password) or ``ENERGY_SMARD_BASE_URL=``
    is an intentional override, not an absence.
    """
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def _env_int(*names: str, default: str) -> int:
    """Like :func:`_env`, but parsed as an int with a named error on malformed input."""
    raw = _env(*names, default=default)
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"expected an integer for {' / '.join(names)}, got {raw!r}") from exc


def _env_float(*names: str, default: str) -> float:
    """Like :func:`_env`, but parsed as a float with a named error on malformed input."""
    raw = _env(*names, default=default)
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"expected a number for {' / '.join(names)}, got {raw!r}") from exc


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


def _env_bool(*names: str, default: str) -> bool:
    """Like :func:`_env`, but parsed as a boolean from a small set of accepted spellings."""
    raw = _env(*names, default=default).strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    raise ValueError(f"expected a boolean for {' / '.join(names)}, got {raw!r}")


def _parse_entity_map(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse ``dataset=entity,dataset=entity`` into a hashable tuple of pairs.

    Kept as a tuple (not a dict) so the enclosing config dataclass stays frozen and hashable.
    Empty input yields an empty mapping (the disabled-by-default case).
    """
    pairs: list[tuple[str, str]] = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        key, sep, value = token.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"malformed ENERGY_HA_ENTITY_MAP entry {token!r}; want dataset=entity")
        pairs.append((key.strip(), value.strip()))
    return tuple(pairs)


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    """Connection settings for the Postgres database, and the schemas the platform uses in it.

    Defaults mirror the compose stack (``dagster`` / ``dagster`` on ``localhost:5432``),
    so a freshly cloned repo needs no configuration. ``ENERGY_PG_*`` overrides win over
    the shared ``DAGSTER_POSTGRES_*`` variables, which win over the defaults.

    ``schema`` is the append-only raw zone the connectors write. ``marts_schema`` is where dbt
    materialises its marts -- the dispatch optimiser *reads* it and never writes there.
    ``derived_schema`` is where the optimiser's own results land for dbt to read back as a source;
    it must match the ``derived`` source's schema in ``dbt/models/staging/_sources.yml``, which
    resolves the same ``ENERGY_DERIVED_SCHEMA`` variable.

    ``staging_schema`` is read by exactly one caller: M7's backtester, which needs the forecast
    vintages that ``stg_weather_forecast`` pivots. It reads that view rather than the raw table
    on purpose -- ``tests/dbt/test_no_lookahead.py`` permits exactly one *dbt model* to select from
    ``raw.forecast_observations``, and going around it in Python would honour the letter of that
    guard while defeating its point.
    """

    host: str = "localhost"
    port: int = 5432
    user: str = "dagster"
    password: str = "dagster"
    database: str = "dagster"
    schema: str = DEFAULT_RAW_SCHEMA
    marts_schema: str = DEFAULT_MARTS_SCHEMA
    staging_schema: str = DEFAULT_STAGING_SCHEMA
    derived_schema: str = DEFAULT_DERIVED_SCHEMA

    @classmethod
    def from_env(cls) -> PostgresConfig:
        return cls(
            host=_env("ENERGY_PG_HOST", "DAGSTER_POSTGRES_HOST", default="localhost"),
            port=_env_int("ENERGY_PG_PORT", "DAGSTER_POSTGRES_PORT", default="5432"),
            user=_env("ENERGY_PG_USER", "DAGSTER_POSTGRES_USER", default="dagster"),
            password=_env("ENERGY_PG_PASSWORD", "DAGSTER_POSTGRES_PASSWORD", default="dagster"),
            database=_env("ENERGY_PG_DB", "DAGSTER_POSTGRES_DB", default="dagster"),
            schema=_env("ENERGY_RAW_SCHEMA", default=DEFAULT_RAW_SCHEMA),
            marts_schema=_env("ENERGY_MARTS_SCHEMA", default=DEFAULT_MARTS_SCHEMA),
            staging_schema=_env("ENERGY_STAGING_SCHEMA", default=DEFAULT_STAGING_SCHEMA),
            derived_schema=_env("ENERGY_DERIVED_SCHEMA", default=DEFAULT_DERIVED_SCHEMA),
        )

    @property
    def dsn(self) -> str:
        """libpq-style connection string for psycopg 3."""
        return (
            f"postgresql://{quote(self.user)}:{quote(self.password)}"
            f"@{self.host}:{self.port}/{quote(self.database)}"
        )


@dataclass(frozen=True, slots=True)
class SmardConfig:
    """Settings for the SMARD HTTP client."""

    base_url: str = DEFAULT_SMARD_BASE_URL
    timeout_seconds: float = 30.0
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> SmardConfig:
        return cls(
            base_url=_env("ENERGY_SMARD_BASE_URL", default=DEFAULT_SMARD_BASE_URL),
            timeout_seconds=_env_float("ENERGY_SMARD_TIMEOUT", default="30"),
            max_retries=_env_int("ENERGY_SMARD_MAX_RETRIES", default="3"),
        )


@dataclass(frozen=True, slots=True)
class Site:
    """A physical location weather is ingested for, identified by a short id.

    The ``id`` is used as the raw-zone ``region`` for weather rows, exactly as ``"DE"`` is
    for market data -- so a weather series is keyed by *where* without ever storing a precise
    address. Coordinates are rounded to ~2 dp; see the module-level note.
    """

    id: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """The site(s) weather is ingested for.

    A single default site today, held in a tuple so more can be added without touching the
    connectors, which resolve coordinates by site id.
    """

    sites: tuple[Site, ...]
    default_id: str

    @classmethod
    def from_env(cls) -> SiteConfig:
        site = Site(
            id=_env("ENERGY_SITE_ID", default=DEFAULT_SITE_ID),
            latitude=_env_float("ENERGY_SITE_LAT", default=str(DEFAULT_SITE_LATITUDE)),
            longitude=_env_float("ENERGY_SITE_LON", default=str(DEFAULT_SITE_LONGITUDE)),
        )
        return cls(sites=(site,), default_id=site.id)

    @property
    def coordinates(self) -> dict[str, tuple[float, float]]:
        """Site id -> ``(latitude, longitude)`` -- the single source of the map the connectors
        (and the Dagster resources) resolve coordinates from, so no call site rebuilds it."""
        return {site.id: (site.latitude, site.longitude) for site in self.sites}

    @property
    def default(self) -> Site:
        return self.resolve(self.default_id)

    def resolve(self, site_id: str) -> Site:
        for site in self.sites:
            if site.id == site_id:
                return site
        known = ", ".join(site.id for site in self.sites)
        raise KeyError(f"unknown site '{site_id}'; configured sites: {known}")


@dataclass(frozen=True, slots=True)
class OpenMeteoConfig:
    """Settings for the Open-Meteo archive and forecast HTTP clients."""

    archive_url: str = DEFAULT_OPEN_METEO_ARCHIVE_URL
    forecast_url: str = DEFAULT_OPEN_METEO_FORECAST_URL
    timeout_seconds: float = 30.0
    max_retries: int = 3
    forecast_days: int = DEFAULT_FORECAST_DAYS

    @classmethod
    def from_env(cls) -> OpenMeteoConfig:
        return cls(
            archive_url=_env(
                "ENERGY_OPEN_METEO_ARCHIVE_URL", default=DEFAULT_OPEN_METEO_ARCHIVE_URL
            ),
            forecast_url=_env(
                "ENERGY_OPEN_METEO_FORECAST_URL", default=DEFAULT_OPEN_METEO_FORECAST_URL
            ),
            timeout_seconds=_env_float("ENERGY_OPEN_METEO_TIMEOUT", default="30"),
            max_retries=_env_int("ENERGY_OPEN_METEO_MAX_RETRIES", default="3"),
            forecast_days=_env_int("ENERGY_FORECAST_DAYS", default=str(DEFAULT_FORECAST_DAYS)),
        )


@dataclass(frozen=True, slots=True)
class PvSystemConfig:
    """The PV array + inverter, as used by the synthetic generator (and later M6).

    ``dc_kwp`` is the panel nameplate; ``ac_cap_kw`` is the *inverter's* AC ceiling -- a
    distinct property, deliberately kept separate so the model can clip production the way a
    real inverter does rather than pretending the DC rating is deliverable. ``performance_ratio``
    (~0.80-0.85 conventionally) folds soiling, wiring, and inverter losses into one factor;
    ``temp_coeff_per_c`` (~-0.004 /degC for silicon) is the linear power derate away from the
    25 degC reference cell temperature.

    ``tilt_deg`` / ``azimuth_deg`` are used only by M7's pvlib plane-of-array model. The M3
    generator ignores them, and that asymmetry is load-bearing rather than an oversight: M3's PV is
    a flat-plate function of *horizontal* irradiance, so on synthetic data the tilted model is
    biased against a truth that has no tilt. See the M7 README section -- the toy model is the
    oracle there, not a competitor.
    """

    dc_kwp: float = DEFAULT_PV_DC_KWP
    ac_cap_kw: float = DEFAULT_PV_AC_CAP_KW
    performance_ratio: float = DEFAULT_PV_PERFORMANCE_RATIO
    temp_coeff_per_c: float = DEFAULT_PV_TEMP_COEFF_PER_C
    tilt_deg: float = DEFAULT_PV_TILT_DEG
    azimuth_deg: float = DEFAULT_PV_AZIMUTH_DEG

    @classmethod
    def from_env(cls) -> PvSystemConfig:
        return cls(
            dc_kwp=_env_float("ENERGY_PV_DC_KWP", default=str(DEFAULT_PV_DC_KWP)),
            ac_cap_kw=_env_float("ENERGY_PV_AC_CAP_KW", default=str(DEFAULT_PV_AC_CAP_KW)),
            performance_ratio=_env_float(
                "ENERGY_PV_PERFORMANCE_RATIO", default=str(DEFAULT_PV_PERFORMANCE_RATIO)
            ),
            temp_coeff_per_c=_env_float(
                "ENERGY_PV_TEMP_COEFF_PER_C", default=str(DEFAULT_PV_TEMP_COEFF_PER_C)
            ),
            tilt_deg=_env_float("ENERGY_PV_TILT_DEG", default=str(DEFAULT_PV_TILT_DEG)),
            azimuth_deg=_env_float("ENERGY_PV_AZIMUTH_DEG", default=str(DEFAULT_PV_AZIMUTH_DEG)),
        )


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    """The battery, as simulated under naive self-consumption in M3 and optimised in M6.

    This is the single physics config shared across the "actual behaviour" baseline and the
    optimiser, so the headline savings number is a like-for-like comparison, not an artefact of
    two different battery models. ``soc_min``/``soc_max`` are the usable fraction of
    ``capacity_kwh``; ``round_trip_efficiency`` is split symmetrically into a charge and a
    discharge leg (each ``sqrt(rte)``) in the simulation.
    """

    capacity_kwh: float = DEFAULT_BATTERY_CAPACITY_KWH
    soc_min: float = 0.05
    soc_max: float = 1.0
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    round_trip_efficiency: float = 0.90

    @classmethod
    def from_env(cls) -> BatteryConfig:
        return cls(
            capacity_kwh=_env_float(
                "ENERGY_BATTERY_CAPACITY_KWH", default=str(DEFAULT_BATTERY_CAPACITY_KWH)
            ),
            soc_min=_env_float("ENERGY_BATTERY_SOC_MIN", default="0.05"),
            soc_max=_env_float("ENERGY_BATTERY_SOC_MAX", default="1.0"),
            max_charge_kw=_env_float("ENERGY_BATTERY_MAX_CHARGE_KW", default="5.0"),
            max_discharge_kw=_env_float("ENERGY_BATTERY_MAX_DISCHARGE_KW", default="5.0"),
            round_trip_efficiency=_env_float("ENERGY_BATTERY_RTE", default="0.90"),
        )


@dataclass(frozen=True, slots=True)
class ThermalConfig:
    """The conditioned zone and its air conditioner -- M3's battery physics, for heat.

    Read by the M10 synthetic generator to drive a single-zone RC model and a bang-bang
    thermostat, and (from M11) by the thermal optimiser that will treat the same zone as storage.
    Sharing one config across both is the discipline that keeps M6's savings comparison honest,
    applied to the thermal side before there is a second side to disagree with.

    **One zone, one unit, linear.** ``r_k_per_kw`` and ``c_kwh_per_k`` give a free-running time
    constant ``R*C``; ``solar_gain_kw_per_wm2`` converts GHI directly into a heat gain, collapsing
    glazing area, orientation and shading into one identifiable number instead of three
    unidentifiable ones. The AC is modelled as fully on or fully off at ``rated_kw`` electrical,
    delivering ``rated_kw * cop`` of cooling -- no part-load curve, because a part-load curve is a
    parameter M11 would have to fit and nothing in M10 could validate.

    ``heating_setpoint_c`` is a **floor, not a load**. The house has non-electric heating, so it
    bounds the zone temperature from below and contributes nothing to ``ac_power`` or to
    ``household_load``. Modelling heating as a dispatchable load is out of scope.

    There is deliberately **no initial-temperature knob**: each Berlin day starts the zone at
    ``setpoint_c``. See the generator docstring for why that is a contract and not a default.
    """

    r_k_per_kw: float = DEFAULT_THERMAL_R_K_PER_KW
    c_kwh_per_k: float = DEFAULT_THERMAL_C_KWH_PER_K
    solar_gain_kw_per_wm2: float = DEFAULT_THERMAL_SOLAR_GAIN_KW_PER_WM2
    setpoint_c: float = DEFAULT_THERMOSTAT_SETPOINT_C
    deadband_k: float = DEFAULT_THERMOSTAT_DEADBAND_K
    heating_setpoint_c: float = DEFAULT_HEATING_SETPOINT_C
    cop: float = DEFAULT_AC_COP
    rated_kw: float = DEFAULT_AC_RATED_KW

    @classmethod
    def from_env(cls) -> ThermalConfig:
        return cls(
            r_k_per_kw=_env_float(
                "ENERGY_THERMAL_R_K_PER_KW", default=str(DEFAULT_THERMAL_R_K_PER_KW)
            ),
            c_kwh_per_k=_env_float(
                "ENERGY_THERMAL_C_KWH_PER_K", default=str(DEFAULT_THERMAL_C_KWH_PER_K)
            ),
            solar_gain_kw_per_wm2=_env_float(
                "ENERGY_THERMAL_SOLAR_GAIN_KW_PER_WM2",
                default=str(DEFAULT_THERMAL_SOLAR_GAIN_KW_PER_WM2),
            ),
            setpoint_c=_env_float(
                "ENERGY_THERMOSTAT_SETPOINT_C", default=str(DEFAULT_THERMOSTAT_SETPOINT_C)
            ),
            deadband_k=_env_float(
                "ENERGY_THERMOSTAT_DEADBAND_K", default=str(DEFAULT_THERMOSTAT_DEADBAND_K)
            ),
            heating_setpoint_c=_env_float(
                "ENERGY_HEATING_SETPOINT_C", default=str(DEFAULT_HEATING_SETPOINT_C)
            ),
            cop=_env_float("ENERGY_AC_COP", default=str(DEFAULT_AC_COP)),
            rated_kw=_env_float("ENERGY_AC_RATED_KW", default=str(DEFAULT_AC_RATED_KW)),
        )


@dataclass(frozen=True, slots=True)
class SyntheticConfig:
    """Parameters of the synthetic telemetry generator.

    ``salt`` seeds the per-day RNG (see the generator docstring): it is part of the data
    contract, because changing it changes every emitted value and therefore every content hash.
    ``annual_load_kwh`` scales the deterministic household load profile.
    """

    salt: str = "energy-platform-synthetic-v1"
    annual_load_kwh: float = 4000.0

    @classmethod
    def from_env(cls) -> SyntheticConfig:
        return cls(
            salt=_env("ENERGY_SYNTHETIC_SALT", default="energy-platform-synthetic-v1"),
            annual_load_kwh=_env_float("ENERGY_SYNTHETIC_ANNUAL_LOAD_KWH", default="4000"),
        )


# Telemetry producers, by the `source` they write into the raw zone. Spelled as literals rather
# than imported from the connectors, because config is the bottom layer everything else imports;
# the connectors' own SOURCE constants are the authority and these must match them.
TELEMETRY_SOURCE_SYNTHETIC = "synthetic"
TELEMETRY_SOURCE_FENECON = "fenecon"

# Forecast vintage producers, by the `source` they write. Deliberately the same spelling as the
# telemetry producers where they coincide: 'synthetic' means reconstructed either way.
FORECAST_SOURCE_SYNTHETIC = "synthetic"
FORECAST_SOURCE_OPEN_METEO = "open_meteo"

DEFAULT_FORECAST_SEED = 0
DEFAULT_MIN_TRAIN_DAYS = 28
DEFAULT_FOLD_STRIDE_DAYS = 7
DEFAULT_ARTIFACT_DIR = "artifacts/forecasting"


def _default_lag_hours(telemetry_source: str) -> int:
    """How long after an hour its telemetry becomes knowable, per producer.

    Synthetic telemetry is generated *from* ingested archive weather, so it inherits ERA5's settling
    delay in full -- it cannot exist before the weather behind it does. A real house has no such
    delay: Home Assistant reports the current instant.
    """
    return 0 if telemetry_source == TELEMETRY_SOURCE_FENECON else DEFAULT_TELEMETRY_LAG_HOURS


def _default_forecast_source(telemetry_source: str) -> str:
    """Which vintage producer pairs with a telemetry producer.

    Derived rather than configured independently because the two are not free to disagree: scoring
    a real house against reconstructed vintages -- or the simulator against forecasts issued for a
    different reality -- measures nothing. It stays overridable for the one case that is coherent,
    scoring synthetic telemetry against genuinely accumulated Open-Meteo vintages once enough of
    them exist.
    """
    return (
        FORECAST_SOURCE_OPEN_METEO
        if telemetry_source == TELEMETRY_SOURCE_FENECON
        else FORECAST_SOURCE_SYNTHETIC
    )


@dataclass(frozen=True, slots=True)
class ForecastConfig:
    """The M7 backtester's parameters.

    ``telemetry_source`` mirrors the dbt var of the same name and selects which producer's history
    the models are trained and scored against. It also decides three things that must never be set
    independently of it:

    * ``forecast_source`` -- which vintage producer supplies the weather features
      (see :func:`_default_forecast_source`). Note there is deliberately **no dbt var** for this:
      ``stg_weather_forecast`` already carries ``source`` in its grain, and no dbt model ever
      collapses the vintage dimension, so the only place the choice has to be made is where the
      vintages are actually read -- here.

    * ``telemetry_lag_hours`` -- the observation lag (see :func:`_default_lag_hours`). Overridable,
      because a real deployment might buffer, but the default is derived so it cannot drift from
      ``ARCHIVE_LAG_DAYS``.
    * ``training_data_source`` -- whether a fitted model saw simulated or real physics. This is
      stamped into every artifact and every prediction row, and real-mode prediction *refuses* a
      synthetic-trained artifact. A residual model fitted on synthetic windows has learned to undo
      a plane-of-array transposition that a real tilted array does not need undone; outside the
      simulator that is not knowledge, it is a systematic error with a confident face on it.

    ``seed`` and the fold geometry are part of the reproducibility statement rather than tuning
    knobs: they are hashed into ``config_hash`` so a prediction row names the experiment that
    produced it.
    """

    telemetry_source: str = TELEMETRY_SOURCE_SYNTHETIC
    forecast_source: str = FORECAST_SOURCE_SYNTHETIC
    telemetry_lag_hours: int = DEFAULT_TELEMETRY_LAG_HOURS
    seed: int = DEFAULT_FORECAST_SEED
    min_train_days: int = DEFAULT_MIN_TRAIN_DAYS
    fold_stride_days: int = DEFAULT_FOLD_STRIDE_DAYS
    artifact_dir: str = DEFAULT_ARTIFACT_DIR

    @classmethod
    def from_env(cls) -> ForecastConfig:
        telemetry_source = _env("ENERGY_TELEMETRY_SOURCE", default=TELEMETRY_SOURCE_SYNTHETIC)
        return cls(
            telemetry_source=telemetry_source,
            forecast_source=_env(
                "ENERGY_FORECAST_SOURCE", default=_default_forecast_source(telemetry_source)
            ),
            telemetry_lag_hours=_env_int(
                "ENERGY_TELEMETRY_LAG_HOURS", default=str(_default_lag_hours(telemetry_source))
            ),
            seed=_env_int("ENERGY_FORECAST_SEED", default=str(DEFAULT_FORECAST_SEED)),
            min_train_days=_env_int(
                "ENERGY_FORECAST_MIN_TRAIN_DAYS", default=str(DEFAULT_MIN_TRAIN_DAYS)
            ),
            fold_stride_days=_env_int(
                "ENERGY_FORECAST_FOLD_STRIDE_DAYS", default=str(DEFAULT_FOLD_STRIDE_DAYS)
            ),
            artifact_dir=_env("ENERGY_FORECAST_ARTIFACT_DIR", default=DEFAULT_ARTIFACT_DIR),
        )

    @property
    def training_data_source(self) -> str:
        """``'synthetic'`` or ``'real'`` -- the provenance stamp carried by every artifact."""
        return (
            TELEMETRY_SOURCE_SYNTHETIC
            if self.telemetry_source == TELEMETRY_SOURCE_SYNTHETIC
            else "real"
        )


@dataclass(frozen=True, slots=True)
class TariffConfig:
    """Which tariffs to price with.

    Deliberately holds no prices: every tariff *parameter* lives in the committed catalogue CSV
    that dbt also seeds (see :mod:`energy_platform.tariffs.catalog`), so a rate can never differ
    between the Python engine and the SQL marts. This config only selects rows from it, and the
    ids must match the dbt vars of the same names (``consumption_tariff_id`` corresponds to the
    tariffs the counterfactual mart prices; ``feed_in_tariff_id`` to the one it compensates
    exports with).

    Nor does it hold the catalogue's *location*: :func:`energy_platform.tariffs.catalog.
    catalog_path` resolves ``ENERGY_TARIFF_CATALOG`` at load time and is the single reader.
    Snapshotting it here too would give the same setting two representations, free to disagree
    once anything touches the environment between construction and load.
    """

    consumption_tariff_id: str = DEFAULT_CONSUMPTION_TARIFF_ID
    feed_in_tariff_id: str = DEFAULT_FEED_IN_TARIFF_ID

    @classmethod
    def from_env(cls) -> TariffConfig:
        return cls(
            consumption_tariff_id=_env("ENERGY_TARIFF_ID", default=DEFAULT_CONSUMPTION_TARIFF_ID),
            feed_in_tariff_id=_env("ENERGY_FEED_IN_TARIFF_ID", default=DEFAULT_FEED_IN_TARIFF_ID),
        )


@dataclass(frozen=True, slots=True)
class HomeAssistantConfig:
    """The real-telemetry connector's settings -- disabled by default.

    Credentials and host come *only* from the environment and are never committed. The
    connector refuses to run unless ``enabled`` is set and a base URL and token are present, so
    the public repo and CI can never accidentally reach a live house. ``entity_map`` maps each
    telemetry :class:`~energy_platform.connectors.types.Dataset` value to a Home Assistant
    entity id, parsed from ``ENERGY_HA_ENTITY_MAP`` as ``dataset=entity,dataset=entity,...``.
    """

    enabled: bool = False
    base_url: str = ""
    token: str = ""
    verify_tls: bool = True
    entity_map: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_env(cls) -> HomeAssistantConfig:
        return cls(
            enabled=_env_bool("ENERGY_HA_ENABLED", default="0"),
            base_url=_env("ENERGY_HA_URL", default=""),
            token=_env("ENERGY_HA_TOKEN", default=""),
            verify_tls=_env_bool("ENERGY_HA_VERIFY_TLS", default="1"),
            entity_map=_parse_entity_map(_env("ENERGY_HA_ENTITY_MAP", default="")),
        )

    @property
    def entities(self) -> dict[str, str]:
        """Dataset value -> entity id (materialised from the hashable tuple form)."""
        return dict(self.entity_map)


@dataclass(frozen=True, slots=True)
class MqttConfig:
    """The M10 plan publisher's broker settings -- disabled by default.

    Deliberately shaped like :class:`HomeAssistantConfig`, because it carries the same risk from
    the other direction: that connector is the one place the platform reaches into a real house,
    and this is the one place it *speaks* to one. Host and credentials come only from the
    environment, the publisher refuses to run unless ``enabled`` is set with a host present, and
    nothing it publishes is a command -- see :mod:`energy_platform.publishing.contract`.

    ``retain`` is on by default and is load-bearing rather than a tuning knob: a retained plan is
    re-read by Home Assistant after a restart without the platform having to be awake, and
    republishing overwrites the one retained message instead of appending to a stream.
    """

    enabled: bool = False
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    topic_prefix: str = "energy"
    qos: int = 1
    retain: bool = True
    discovery_enabled: bool = True
    discovery_prefix: str = "homeassistant"
    client_id: str = "energy-platform-publisher"
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> MqttConfig:
        return cls(
            enabled=_env_bool("ENERGY_MQTT_ENABLED", default="0"),
            host=_env("ENERGY_MQTT_HOST", default=""),
            port=_env_int("ENERGY_MQTT_PORT", default="1883"),
            username=_env("ENERGY_MQTT_USER", default=""),
            password=_env("ENERGY_MQTT_PASSWORD", default=""),
            tls=_env_bool("ENERGY_MQTT_TLS", default="0"),
            topic_prefix=_env("ENERGY_MQTT_TOPIC_PREFIX", default="energy"),
            qos=_env_int("ENERGY_MQTT_QOS", default="1"),
            retain=_env_bool("ENERGY_MQTT_RETAIN", default="1"),
            discovery_enabled=_env_bool("ENERGY_MQTT_DISCOVERY_ENABLED", default="1"),
            discovery_prefix=_env("ENERGY_MQTT_DISCOVERY_PREFIX", default="homeassistant"),
            client_id=_env("ENERGY_MQTT_CLIENT_ID", default="energy-platform-publisher"),
            timeout_seconds=_env_float("ENERGY_MQTT_TIMEOUT_SECONDS", default="10"),
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Top-level configuration bundle."""

    postgres: PostgresConfig
    smard: SmardConfig
    open_meteo: OpenMeteoConfig
    site: SiteConfig
    pv: PvSystemConfig
    battery: BatteryConfig
    thermal: ThermalConfig
    synthetic: SyntheticConfig
    forecast: ForecastConfig
    tariffs: TariffConfig
    home_assistant: HomeAssistantConfig
    mqtt: MqttConfig

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            postgres=PostgresConfig.from_env(),
            smard=SmardConfig.from_env(),
            open_meteo=OpenMeteoConfig.from_env(),
            site=SiteConfig.from_env(),
            pv=PvSystemConfig.from_env(),
            battery=BatteryConfig.from_env(),
            thermal=ThermalConfig.from_env(),
            synthetic=SyntheticConfig.from_env(),
            forecast=ForecastConfig.from_env(),
            tariffs=TariffConfig.from_env(),
            home_assistant=HomeAssistantConfig.from_env(),
            mqtt=MqttConfig.from_env(),
        )
