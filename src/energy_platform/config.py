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
    def mapping(self) -> dict[str, Site]:
        """Site id -> :class:`Site`, for the connectors' coordinate lookup."""
        return {site.id: site for site in self.sites}

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
class AppConfig:
    """Top-level configuration bundle."""

    postgres: PostgresConfig
    smard: SmardConfig
    open_meteo: OpenMeteoConfig
    site: SiteConfig

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            postgres=PostgresConfig.from_env(),
            smard=SmardConfig.from_env(),
            open_meteo=OpenMeteoConfig.from_env(),
            site=SiteConfig.from_env(),
        )
