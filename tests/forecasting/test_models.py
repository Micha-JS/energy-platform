"""The two physical PV models, the quantile forecaster, and the provenance interlock.

The load-bearing assertion here is the first one: on synthetic data M3's toy model must be
*bit-identical* to the process that produced the labels. If it drifts even slightly, the oracle row
in ``mart_forecast_eval`` stops being the oracle and the whole honesty framing becomes a little bit
false. Delegation makes it true by construction; this pins it anyway, because that is the kind of
thing a later refactor "simplifies".

Nothing here touches Postgres or the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from energy_platform.config import ForecastConfig, PvSystemConfig
from energy_platform.connectors.synthetic import pv_production_kwh
from energy_platform.forecasting.models import (
    HYPERPARAMETERS,
    QUANTILES,
    ModelCard,
    ProvenanceError,
    fit_quantile_forecaster,
    irreducible_load_mae_kwh,
    library_versions,
    load_artifact,
    pinball_loss,
    save_artifact,
)
from energy_platform.forecasting.physical import (
    plane_of_array_wm2,
    pvlib_ac_kwh,
    recover_dni,
    solar_geometry,
    toy_ac_kwh,
)
from energy_platform.forecasting.vintage import SELECTION_RULE_ID

PV = PvSystemConfig()
LAT, LON = 52.52, 13.40

# A clear late-April day: shoulder season, where a tilted plane gains most over a horizontal one.
_GHI = np.array(
    [0, 0, 0, 0, 5, 45, 130, 250, 380, 500, 590, 640,
     650, 620, 550, 440, 310, 175, 60, 5, 0, 0, 0, 0],
    dtype=float,
)  # fmt: skip
_DIRECT_H = _GHI * 0.68
_DIFFUSE = _GHI * 0.32
_TEMP = np.full(24, 14.0)
_WIND = np.full(24, 3.2)


def _day_ms(day: datetime) -> np.ndarray:
    base = int(day.timestamp() * 1000)
    return np.array([base + hour * 3_600_000 for hour in range(24)], dtype=np.int64)


TS_MS = _day_ms(datetime(2024, 4, 20, 0, 0, tzinfo=UTC))
GEOMETRY = solar_geometry(TS_MS, LAT, LON)


# -- The oracle claim --------------------------------------------------------------------


def test_the_toy_model_is_bit_identical_to_the_generator_that_made_the_labels() -> None:
    """On synthetic data this function IS the data-generating process, not an approximation.

    ``mart_forecast_eval`` marks its row ``role='oracle'`` on that basis. A reimplementation that
    drifted by a rounding step would make that label a small lie, and the milestone's whole framing
    rests on it being exact.
    """
    vectorised = toy_ac_kwh(_GHI, _TEMP, PV)
    one_at_a_time = [
        pv_production_kwh(float(g), float(t), PV) for g, t in zip(_GHI, _TEMP, strict=True)
    ]
    assert vectorised.tolist() == one_at_a_time


def test_the_toy_model_propagates_missing_irradiance() -> None:
    ghi = _GHI.copy()
    ghi[10] = np.nan
    assert np.isnan(toy_ac_kwh(ghi, _TEMP, PV)[10])


def test_the_toy_model_handles_missing_temperature_as_no_derate() -> None:
    """Matching M3, which treats absent temperature as a derate of 1.0 rather than as no output."""
    temp = _TEMP.copy()
    temp[12] = np.nan
    assert toy_ac_kwh(_GHI, temp, PV)[12] == pv_production_kwh(float(_GHI[12]), None, PV)


# -- The artefact the milestone is honest about -------------------------------------------


def test_the_tilted_model_collects_more_than_the_flat_plate_one_in_shoulder_season() -> None:
    """The systematic gap that makes the seeded comparison unwinnable for the physical model.

    A 35-degree plane facing south gathers materially more than a horizontal one in April, which is
    correct physics and exactly wrong against a truth generated with no tilt at all. Asserted rather
    than described, so the README's claim about the ranking has a number behind it that moves if the
    orientation config does.
    """
    tilted = pvlib_ac_kwh(_GHI, _DIRECT_H, _DIFFUSE, _TEMP, _WIND, GEOMETRY, PV).sum()
    flat = toy_ac_kwh(_GHI, _TEMP, PV).sum()
    assert tilted > flat * 1.05, f"tilted {tilted:.2f} kWh vs flat {flat:.2f} kWh"


def test_plane_of_array_exceeds_horizontal_irradiance_in_shoulder_season() -> None:
    poa = plane_of_array_wm2(_GHI, _DIRECT_H, _DIFFUSE, GEOMETRY, PV)
    assert np.nansum(poa) > _GHI.sum()


# -- DNI recovered exactly, not decomposed ------------------------------------------------


def test_dni_recovery_inverts_the_projection_it_undoes() -> None:
    """``direct_horizontal == DNI * cos(zenith)`` is an identity, so recovery must round-trip."""
    dni = recover_dni(_DIRECT_H, GEOMETRY)
    daylight = GEOMETRY.zenith_deg < 85.0
    reprojected = dni * GEOMETRY.cos_zenith
    assert np.allclose(reprojected[daylight], _DIRECT_H[daylight], atol=1e-9)


def test_dni_is_zeroed_rather_than_exploding_at_low_sun() -> None:
    """``1/cos(zenith)`` diverges at the horizon; the diffuse fraction carries those hours."""
    dni = recover_dni(_DIRECT_H, GEOMETRY)
    assert np.all(np.isfinite(dni))
    assert np.all(dni[GEOMETRY.zenith_deg >= 85.0] == 0.0)


def test_the_pvlib_chain_propagates_missing_weather() -> None:
    ghi = _GHI.copy()
    ghi[11] = np.nan
    produced = pvlib_ac_kwh(ghi, _DIRECT_H, _DIFFUSE, _TEMP, _WIND, GEOMETRY, PV)
    assert np.isnan(produced[11])


def test_output_never_exceeds_the_inverters_ac_ceiling() -> None:
    """`inverter.pvwatts`'s pdc0 is the DC input limit, not the AC cap -- an easy 4% error."""
    blazing = np.full(24, 1100.0)
    produced = pvlib_ac_kwh(blazing, blazing * 0.8, blazing * 0.2, _TEMP, _WIND, GEOMETRY, PV)
    assert produced.max() <= PV.ac_cap_kw + 1e-9
    assert produced.max() > PV.ac_cap_kw * 0.95, "the cap should be reached, not merely respected"


# -- The quantile forecaster --------------------------------------------------------------


def _card(**overrides: object) -> ModelCard:
    base: dict[str, object] = {
        "model_key": "load_hgb",
        "target": "household_load_kwh",
        "site": "home",
        "training_data_source": "synthetic",
        "selection_rule_id": SELECTION_RULE_ID,
        "telemetry_lag_hours": 120,
        "train_start": "2024-03-28",
        "train_end": "2024-05-31",
        "feature_names": ("local_hour", "weekday"),
        "hyperparameters": HYPERPARAMETERS,
        "quantiles": QUANTILES,
        "library_versions": library_versions(),
        "seed": 0,
        "min_train_days": 28,
        "fold_stride_days": 7,
    }
    base.update(overrides)
    return ModelCard(**base)  # type: ignore[arg-type]


def _training_set() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    hours = np.tile(np.arange(24, dtype=float), 60)
    weekdays = np.repeat(np.arange(60, dtype=float) % 7, 24)
    features = np.column_stack([hours, weekdays])
    target = 0.3 + 0.4 * np.sin(hours / 24 * 2 * np.pi) + rng.normal(0, 0.02, hours.size)
    return features, target


def test_quantiles_come_back_ordered() -> None:
    features, target = _training_set()
    predictions = fit_quantile_forecaster(features, target, _card()).predict(features[:50])
    assert predictions.shape == (50, len(QUANTILES))
    assert np.all(np.diff(predictions, axis=1) >= 0)


def test_a_refit_reproduces_bit_for_bit_at_a_pinned_thread_count() -> None:
    """The honest determinism claim: same threads and versions, identical predictions."""
    features, target = _training_set()
    first = fit_quantile_forecaster(features, target, _card()).predict(features[:100])
    second = fit_quantile_forecaster(features, target, _card()).predict(features[:100])
    assert np.array_equal(first, second)


def test_a_training_set_with_no_finite_target_is_refused() -> None:
    """A constant prediction fitted on nothing would score, badly, and look like a result."""
    features, _ = _training_set()
    with pytest.raises(ValueError, match="nothing to fit"):
        fit_quantile_forecaster(features, np.full(features.shape[0], np.nan), _card())


# -- The config hash ----------------------------------------------------------------------


def test_the_config_hash_covers_inputs_and_ignores_outcomes() -> None:
    """M6's _input_digest reasoning: hash the problem, not the answer."""
    assert _card().config_hash == _card(n_train_rows=999).config_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("train_end", "2024-06-30"),
        ("telemetry_lag_hours", 0),
        ("selection_rule_id", "something_else"),
        ("feature_names", ("local_hour",)),
        ("training_data_source", "real"),
        ("seed", 1),
        # Fold geometry. Two runs differing only here train on different sets and predict
        # differently, so a shared hash would make `derived.forecast_runs` unable to tell the
        # experiments apart -- the one question the hash is there to answer.
        ("min_train_days", 14),
        ("fold_stride_days", 1),
    ],
)
def test_the_config_hash_moves_when_the_experiment_does(field: str, value: object) -> None:
    assert _card(**{field: value}).config_hash != _card().config_hash


# -- The provenance interlock -------------------------------------------------------------


def test_a_synthetic_trained_model_is_refused_for_real_prediction(tmp_path: Path) -> None:
    """The interlock. A model fitted on simulated physics has learned the simulator's corrections.

    For the PV residual specifically it has learned to undo a plane-of-array transposition that a
    real tilted array genuinely needs -- so applying it to a real house would push the error the
    wrong way, confidently, with nothing downstream able to tell.
    """
    config = ForecastConfig(artifact_dir=str(tmp_path))
    features, target = _training_set()
    saved = save_artifact(
        fit_quantile_forecaster(features, target, _card(training_data_source="synthetic")), config
    )

    with pytest.raises(ProvenanceError, match="refusing to load a synthetic-trained model"):
        load_artifact(saved, expected_data_source="real")


def test_a_matching_provenance_loads_and_predicts_identically(tmp_path: Path) -> None:
    """The positive control: the interlock must not be refusing everything."""
    config = ForecastConfig(artifact_dir=str(tmp_path))
    features, target = _training_set()
    forecaster = fit_quantile_forecaster(features, target, _card())
    saved = save_artifact(forecaster, config)

    reloaded = load_artifact(saved, expected_data_source="synthetic")
    assert np.array_equal(reloaded.predict(features[:20]), forecaster.predict(features[:20]))


def test_the_artifact_path_is_keyed_by_the_config_hash(tmp_path: Path) -> None:
    """So a prediction row's config_hash locates the exact model that produced it."""
    config = ForecastConfig(artifact_dir=str(tmp_path))
    features, target = _training_set()
    card = _card()
    saved = save_artifact(fit_quantile_forecaster(features, target, card), config)
    assert saved.name == card.config_hash
    assert saved.parent.name == card.model_key


def test_the_card_is_written_as_readable_json(tmp_path: Path) -> None:
    """A stored model has to be inspectable without loading a pickle."""
    config = ForecastConfig(artifact_dir=str(tmp_path))
    features, target = _training_set()
    saved = save_artifact(fit_quantile_forecaster(features, target, _card()), config)
    payload = json.loads((saved / "card.json").read_text())
    assert payload["selection_rule_id"] == SELECTION_RULE_ID
    assert payload["training_data_source"] == "synthetic"
    # pvlib and pandas as well as scikit-learn: the physical chain and every solar-geometry feature
    # are pvlib output, so a pvlib upgrade moves the mart's numbers and the record has to say so.
    assert {"scikit-learn", "pvlib", "pandas"} <= payload["library_versions"].keys()


# -- Metrics ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "predicted", "quantile", "expected"),
    [
        (5.0, 4.0, 0.5, 0.5),  # under-predicted at the median: 0.5 * 1.0
        (4.0, 5.0, 0.5, 0.5),  # over-predicted at the median, symmetric
        (5.0, 4.0, 0.9, 0.9),  # p90 penalises under-prediction heavily
        (4.0, 5.0, 0.9, 0.1),  # ...and over-prediction lightly
        (3.0, 3.0, 0.1, 0.0),
    ],
)
def test_pinball_loss_is_asymmetric_in_the_direction_the_quantile_implies(
    actual: float, predicted: float, quantile: float, expected: float
) -> None:
    assert pinball_loss(actual, predicted, quantile) == pytest.approx(expected)


def test_the_irreducible_load_error_is_the_generators_own_noise_floor() -> None:
    """E|U(-a, a)| = a/2, so a perfect predictor of the shape still scores this.

    Reported beside the models so 'the GBM crushes persistence' is a bounded claim rather than a
    suspicious one -- nothing can do better than this on seeded data.
    """
    assert irreducible_load_mae_kwh(0.5, 0.12) == pytest.approx(0.03)
