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

# Public Open-Meteo API base URLs (no API key required, CC BY 4.0).
DEFAULT_OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DEFAULT_OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_FORECAST_DAYS = 7

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
    """Connection settings for the raw-zone Postgres database.

    Defaults mirror the compose stack (``dagster`` / ``dagster`` on ``localhost:5432``),
    so a freshly cloned repo needs no configuration. ``ENERGY_PG_*`` overrides win over
    the shared ``DAGSTER_POSTGRES_*`` variables, which win over the defaults.
    """

    host: str = "localhost"
    port: int = 5432
    user: str = "dagster"
    password: str = "dagster"
    database: str = "dagster"
    schema: str = DEFAULT_RAW_SCHEMA

    @classmethod
    def from_env(cls) -> PostgresConfig:
        return cls(
            host=_env("ENERGY_PG_HOST", "DAGSTER_POSTGRES_HOST", default="localhost"),
            port=_env_int("ENERGY_PG_PORT", "DAGSTER_POSTGRES_PORT", default="5432"),
            user=_env("ENERGY_PG_USER", "DAGSTER_POSTGRES_USER", default="dagster"),
            password=_env("ENERGY_PG_PASSWORD", "DAGSTER_POSTGRES_PASSWORD", default="dagster"),
            database=_env("ENERGY_PG_DB", "DAGSTER_POSTGRES_DB", default="dagster"),
            schema=_env("ENERGY_RAW_SCHEMA", default=DEFAULT_RAW_SCHEMA),
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
    """

    dc_kwp: float = DEFAULT_PV_DC_KWP
    ac_cap_kw: float = DEFAULT_PV_AC_CAP_KW
    performance_ratio: float = DEFAULT_PV_PERFORMANCE_RATIO
    temp_coeff_per_c: float = DEFAULT_PV_TEMP_COEFF_PER_C

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
class AppConfig:
    """Top-level configuration bundle."""

    postgres: PostgresConfig
    smard: SmardConfig
    open_meteo: OpenMeteoConfig
    site: SiteConfig
    pv: PvSystemConfig
    battery: BatteryConfig
    synthetic: SyntheticConfig
    home_assistant: HomeAssistantConfig

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            postgres=PostgresConfig.from_env(),
            smard=SmardConfig.from_env(),
            open_meteo=OpenMeteoConfig.from_env(),
            site=SiteConfig.from_env(),
            pv=PvSystemConfig.from_env(),
            battery=BatteryConfig.from_env(),
            synthetic=SyntheticConfig.from_env(),
            home_assistant=HomeAssistantConfig.from_env(),
        )
