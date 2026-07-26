"""The tariff catalogue: the single source of truth for tariff *parameters*.

The catalogue is one committed CSV, ``dbt/seeds/tariffs.csv``, read directly by both layers
that price energy:

* **this module**, so the M6 dispatch optimiser can evaluate tariff cost inside its objective
  (it cannot call SQL per candidate dispatch), and
* **dbt**, as an ordinary seed relation ``{{ ref('tariffs') }}`` that ``dbt build`` loads
  before the models that join it.

One file with two readers, rather than one file rendered into another: a render step would
need a generator plus a CI "re-render and diff" check, which adds a staleness bug class
instead of removing one. Parameters therefore cannot drift. The *arithmetic* is necessarily
implemented twice (here and in the ``tariff_import_price_ct_kwh`` dbt macro), and that pair is
pinned by ``tests/dbt/test_tariff_reconciliation.py``, which recomputes every priced hour of
``int_hourly_tariff_cost`` with this engine and asserts equality.

**Every per-kWh value in the catalogue is net of VAT.** ``vat_rate`` is applied exactly once,
downstream in :mod:`energy_platform.tariffs.engine` -- see that module for the convention.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# ``.../src/energy_platform/tariffs/catalog.py`` -> the repo root is four parents up. The
# catalogue lives with the dbt seeds because that is where dbt must find it; it is genuinely
# shared data, not test data, so the loader validates the path and raises a named error rather
# than trusting it. Outside a source checkout, point ENERGY_TARIFF_CATALOG at a copy.
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[3] / "dbt" / "seeds" / "tariffs.csv"

CATALOG_PATH_ENV = "ENERGY_TARIFF_CATALOG"

# The columns the CSV must carry, in no particular order. A missing column is a malformed
# catalogue, not a null field -- catching it here keeps the failure at load time with a name.
_REQUIRED_COLUMNS = frozenset(
    {
        "tariff_id",
        "label",
        "kind",
        "price_ct_kwh",
        "margin_ct_kwh",
        "pass_through_ct_kwh",
        "base_fee_eur_month",
        "vat_rate",
    }
)


class TariffKind(StrEnum):
    """What a catalogue row prices.

    ``STATIC`` and ``DYNAMIC`` are consumption tariffs and are mutually exclusive alternatives
    for the same household; ``FEED_IN`` is the statutory compensation for exported energy and
    applies *alongside* whichever consumption tariff is in force.
    """

    STATIC = "static"  # flat retail energy price, independent of the wholesale market
    DYNAMIC = "dynamic"  # hourly day-ahead spot + supplier margin + fixed pass-through
    FEED_IN = "feed_in"  # flat compensation per exported kWh (EEG), outside VAT


@dataclass(frozen=True, slots=True)
class TariffSpec:
    """One catalogue row: the parameters of a single tariff.

    All per-kWh values are **net** (excluding VAT), in ct/kWh; ``base_fee_eur_month`` is the
    monthly standing charge (Grundpreis) in EUR, also net. ``vat_rate`` is a fraction
    (0.19 == 19%), applied once by the engine.

    Fields not applicable to a ``kind`` are ``None``: a dynamic tariff has no flat
    ``price_ct_kwh`` (its per-kWh rate is derived from the spot price), and a feed-in row has
    no margin, pass-through, or base fee. :meth:`__post_init__` enforces that, so a
    half-populated row fails at load time rather than silently pricing energy at ``None``.
    """

    tariff_id: str
    label: str
    kind: TariffKind
    price_ct_kwh: float | None = None
    margin_ct_kwh: float | None = None
    pass_through_ct_kwh: float | None = None
    base_fee_eur_month: float | None = None
    vat_rate: float = 0.0

    def __post_init__(self) -> None:
        missing = [name for name in self._required_fields() if getattr(self, name) is None]
        if missing:
            raise ValueError(
                f"tariff {self.tariff_id!r} of kind {self.kind.value!r} is missing required "
                f"field(s): {', '.join(sorted(missing))}"
            )
        if not 0.0 <= self.vat_rate < 1.0:
            raise ValueError(
                f"tariff {self.tariff_id!r}: vat_rate must be a fraction in [0, 1), "
                f"got {self.vat_rate!r} -- 19% is 0.19, not 19"
            )

    def _required_fields(self) -> tuple[str, ...]:
        if self.kind is TariffKind.STATIC:
            return ("price_ct_kwh", "base_fee_eur_month")
        if self.kind is TariffKind.DYNAMIC:
            return ("margin_ct_kwh", "pass_through_ct_kwh", "base_fee_eur_month")
        return ("price_ct_kwh",)

    @property
    def is_consumption(self) -> bool:
        """Whether this row prices imported energy (as opposed to compensating exports)."""
        return self.kind in (TariffKind.STATIC, TariffKind.DYNAMIC)


def catalog_path() -> Path:
    """The catalogue CSV to read: ``ENERGY_TARIFF_CATALOG`` if set, else the committed seed."""
    override = os.environ.get(CATALOG_PATH_ENV)
    return Path(override) if override else DEFAULT_CATALOG_PATH


def load_catalog(path: Path | None = None) -> Mapping[str, TariffSpec]:
    """Load the tariff catalogue, keyed by ``tariff_id``.

    Raises :class:`FileNotFoundError` when the catalogue is absent and :class:`ValueError`
    naming the offending row or column when it is malformed -- the same "fail with a name"
    posture as the ``_env_*`` helpers in :mod:`energy_platform.config`.
    """
    source = path if path is not None else catalog_path()
    if not source.is_file():
        raise FileNotFoundError(
            f"tariff catalogue not found at {source}; set {CATALOG_PATH_ENV} to override"
        )

    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or ())
        if missing := _REQUIRED_COLUMNS - columns:
            raise ValueError(
                f"tariff catalogue {source} is missing column(s): {', '.join(sorted(missing))}"
            )
        specs: dict[str, TariffSpec] = {}
        # ``start=2`` so the reported line number matches the file as an editor shows it
        # (line 1 is the header).
        for line_no, row in enumerate(reader, start=2):
            spec = _spec_from_row(row, source, line_no)
            if spec.tariff_id in specs:
                raise ValueError(
                    f"{source}:{line_no}: duplicate tariff_id {spec.tariff_id!r}; "
                    "ids key the catalogue and must be unique"
                )
            specs[spec.tariff_id] = spec

    if not specs:
        raise ValueError(f"tariff catalogue {source} contains no rows")
    return specs


def _spec_from_row(row: Mapping[str, str | None], source: Path, line_no: int) -> TariffSpec:
    tariff_id = (row.get("tariff_id") or "").strip()
    if not tariff_id:
        raise ValueError(f"{source}:{line_no}: tariff_id is required")
    try:
        kind = TariffKind((row.get("kind") or "").strip())
    except ValueError as exc:
        known = ", ".join(k.value for k in TariffKind)
        raise ValueError(
            f"{source}:{line_no}: tariff {tariff_id!r} has unknown kind "
            f"{(row.get('kind') or '').strip()!r}; expected one of {known}"
        ) from exc

    def number(column: str) -> float | None:
        raw = (row.get(column) or "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(
                f"{source}:{line_no}: tariff {tariff_id!r} column {column} "
                f"expected a number, got {raw!r}"
            ) from exc

    # Parse every number first, so a malformed cell reports its own column rather than being
    # re-wrapped by the __post_init__ handler below.
    price = number("price_ct_kwh")
    margin = number("margin_ct_kwh")
    pass_through = number("pass_through_ct_kwh")
    base_fee = number("base_fee_eur_month")
    vat = number("vat_rate")

    try:
        return TariffSpec(
            tariff_id=tariff_id,
            label=(row.get("label") or "").strip(),
            kind=kind,
            price_ct_kwh=price,
            margin_ct_kwh=margin,
            pass_through_ct_kwh=pass_through,
            base_fee_eur_month=base_fee,
            vat_rate=0.0 if vat is None else vat,
        )
    except ValueError as exc:
        # Re-raise __post_init__ validation with the file position attached.
        raise ValueError(f"{source}:{line_no}: {exc}") from exc


def resolve(tariff_id: str, catalog: Mapping[str, TariffSpec] | None = None) -> TariffSpec:
    """Look up one tariff by id, raising a :class:`KeyError` that lists the known ids."""
    specs = catalog if catalog is not None else load_catalog()
    try:
        return specs[tariff_id]
    except KeyError as exc:
        known = ", ".join(sorted(specs))
        raise KeyError(f"unknown tariff '{tariff_id}'; catalogue holds: {known}") from exc
