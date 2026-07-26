"""The tariff catalogue: loading, validation, and the contract with the dbt seed.

The catalogue is one committed CSV read by both the Python engine and dbt. These tests pin the
half of that contract Python owns -- that the shipped file parses, that every kind carries the
fields its arithmetic needs, and that a malformed row fails at load time with a name rather
than surfacing as a NULL price somewhere downstream. The other half (that dbt and Python agree
about the rows) is asserted by tests/dbt/test_tariff_reconciliation.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from energy_platform.tariffs.catalog import (
    DEFAULT_CATALOG_PATH,
    TariffKind,
    TariffSpec,
    load_catalog,
    resolve,
)

HEADER = (
    "tariff_id,label,kind,price_ct_kwh,margin_ct_kwh,pass_through_ct_kwh,"
    "base_fee_eur_month,vat_rate\n"
)


def _catalog(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "tariffs.csv"
    path.write_text(HEADER + "".join(row + "\n" for row in rows), encoding="utf-8")
    return path


# -- The shipped catalogue ------------------------------------------------------------------


def test_the_committed_catalogue_loads() -> None:
    """The file dbt seeds is the file Python reads; if this fails the two layers have diverged."""
    catalog = load_catalog()
    assert DEFAULT_CATALOG_PATH.is_file()
    assert set(catalog) == {"static_2024", "dynamic_2024", "eeg_2024"}


def test_the_committed_catalogue_covers_every_kind() -> None:
    """M5 prices {static, dynamic} consumption against one feed-in scheme; all three must exist."""
    kinds = {spec.kind for spec in load_catalog().values()}
    assert kinds == {TariffKind.STATIC, TariffKind.DYNAMIC, TariffKind.FEED_IN}


def test_exactly_one_feed_in_scheme_ships() -> None:
    """More than one and the feed_in_tariff_id var stops having a safe default."""
    feed_in = [s for s in load_catalog().values() if s.kind is TariffKind.FEED_IN]
    assert len(feed_in) == 1


# -- Validation: a half-populated row fails at load time, not at pricing time ----------------


def test_static_row_without_a_price_raises_naming_the_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="price_ct_kwh"):
        load_catalog(_catalog(tmp_path, "s,Static,static,,,,10.84,0.19"))


def test_dynamic_row_without_a_margin_raises_naming_the_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="margin_ct_kwh"):
        load_catalog(_catalog(tmp_path, "d,Dynamic,dynamic,,,17.40,10.84,0.19"))


def test_feed_in_row_needs_only_a_rate(tmp_path: Path) -> None:
    """No margin, no pass-through, no standing charge -- compensation is one flat number."""
    catalog = load_catalog(_catalog(tmp_path, "f,Feed-in,feed_in,8.11,,,,0.00"))
    assert catalog["f"].price_ct_kwh == 8.11
    assert catalog["f"].base_fee_eur_month is None


def test_vat_written_as_a_percentage_is_rejected(tmp_path: Path) -> None:
    """19 instead of 0.19 would inflate every bill twentyfold and still look like a number."""
    with pytest.raises(ValueError, match=r"19% is 0\.19, not 19"):
        load_catalog(_catalog(tmp_path, "s,Static,static,29.41,,,10.84,19"))


def test_unknown_kind_raises_listing_the_known_ones(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        load_catalog(_catalog(tmp_path, "x,Mystery,capacity,29.41,,,10.84,0.19"))


def test_non_numeric_rate_raises_naming_the_column_and_line(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="price_ct_kwh expected a number"):
        load_catalog(_catalog(tmp_path, "s,Static,static,cheap,,,10.84,0.19"))


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    """Ids key the catalogue, so a duplicate would silently shadow a tariff."""
    rows = ("s,First,static,29.41,,,10.84,0.19", "s,Second,static,31.00,,,10.84,0.19")
    with pytest.raises(ValueError, match="duplicate tariff_id"):
        load_catalog(_catalog(tmp_path, *rows))


def test_missing_column_is_reported_as_a_malformed_catalogue(tmp_path: Path) -> None:
    path = tmp_path / "tariffs.csv"
    path.write_text("tariff_id,kind\ns,static\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing column"):
        load_catalog(path)


def test_an_empty_catalogue_is_an_error_not_an_empty_mapping(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="contains no rows"):
        load_catalog(_catalog(tmp_path))


def test_absent_catalogue_names_the_override_variable(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="ENERGY_TARIFF_CATALOG"):
        load_catalog(tmp_path / "nope.csv")


# -- Lookup ---------------------------------------------------------------------------------


def test_resolve_returns_the_named_tariff() -> None:
    assert resolve("static_2024").kind is TariffKind.STATIC


def test_resolve_lists_the_known_ids_when_asked_for_a_missing_one() -> None:
    with pytest.raises(KeyError, match="static_2024"):
        resolve("tariff_from_2019")


def test_specs_are_frozen() -> None:
    """Immutable so a caller cannot mutate a shared catalogue entry underneath another."""
    spec = resolve("static_2024")
    with pytest.raises(AttributeError):
        spec.price_ct_kwh = 1.0  # type: ignore[misc]


def test_only_consumption_kinds_report_is_consumption() -> None:
    catalog = load_catalog()
    assert catalog["static_2024"].is_consumption
    assert catalog["dynamic_2024"].is_consumption
    assert not catalog["eeg_2024"].is_consumption


def test_catalog_path_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _catalog(tmp_path, "only,Only tariff,static,30.0,,,10.0,0.19")
    monkeypatch.setenv("ENERGY_TARIFF_CATALOG", str(path))
    assert set(load_catalog()) == {"only"}


def test_a_spec_can_be_built_directly_for_tests() -> None:
    """The engine takes TariffSpec, not ids, so tests and the optimiser can construct one."""
    spec = TariffSpec(
        tariff_id="t", label="", kind=TariffKind.STATIC, price_ct_kwh=30.0, base_fee_eur_month=0.0
    )
    assert spec.is_consumption
