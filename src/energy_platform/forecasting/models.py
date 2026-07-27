"""Gradient-boosted quantile models, their config hash, and the provenance interlock.

``HistGradientBoostingRegressor`` with ``loss="quantile"``, fitted three times per model for
p10/p50/p90. Chosen over LightGBM for reasons argued in ``pyproject.toml``; the two that matter here
are that quantile loss is built in, so pinball loss costs three fits rather than a wrapper, and that
missing values are routed down a learned branch rather than imputed -- "NaN over fabrication"
surviving all the way into the model.

**Determinism, scoped honestly.** The repo already makes two claims: ingestion is bit-reproducible
and content-hashed, optimisation is value-stable but not hash-guaranteed. This is a third and weaker
tier, and the mechanism is not the one you would guess:

    ``random_state`` is nearly inert at this scale. It governs binning subsampling (which does
    nothing below 200 000 samples) and the early-stopping split -- and ``early_stopping='auto'``
    is *off* below 10 000 samples, which ~2 000 hourly rows are. What actually moves the last bits
    of every split threshold is OpenMP: histogram accumulation is threaded, so the float summation
    order follows ``OMP_NUM_THREADS``.

So the thread count is pinned to one for the duration of each fit, ``early_stopping`` is set
explicitly rather than left to a default that changes with the data size, and ``random_state`` is
fixed anyway because a default that does nothing today may do something after an upgrade. The claim
is: **same thread count and library versions, identical predictions; different thread count,
identical metrics to reported precision but not identical bits.** No hash equality is claimed and no
test asserts one.

**The provenance interlock.** A residual model fitted on synthetic windows has learned to undo a
plane-of-array transposition that a real tilted array does not need undone. Outside the simulator
that is not knowledge, it is a systematic error wearing a confident face. So every artifact records
the ``training_data_source`` it was fitted on, and :func:`load_artifact` *refuses* to return a
synthetic-trained model to a real-mode caller. That refusal is a hard error rather than a warning:
the whole point is that it cannot be ignored by someone in a hurry.

The hyperparameters are deliberately off the defaults. The largest PV fold is ~1 300 daylight rows;
``max_leaf_nodes=31, min_samples_leaf=20`` puts ~20 rows in a leaf at full depth, which is
memorisation rather than fitting.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import joblib
import numpy as np
from numpy.typing import NDArray
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits

from energy_platform.config import ForecastConfig

# The quantiles every model emits. p50 is the point forecast MAE is computed against; p10/p90 make
# pinball loss meaningful and give an interval whose calibration is *reported*, never claimed.
QUANTILES: Final[tuple[float, ...]] = (0.10, 0.50, 0.90)

# Tuned down from the defaults for a ~1-2k row training set; see the module docstring.
HYPERPARAMETERS: Final[Mapping[str, Any]] = {
    "max_leaf_nodes": 15,
    "min_samples_leaf": 40,
    "learning_rate": 0.05,
    "max_iter": 200,
    # Explicit rather than 'auto': 'auto' switches on above 10 000 samples, so a default that is
    # off today would silently turn on if the training window ever grew. A promise that changes
    # with the data size is not a promise.
    "early_stopping": False,
    "random_state": 0,
}

# Fits run single-threaded so a re-fit on one machine reproduces bit-for-bit. This is the actual
# determinism lever -- see the module docstring on why random_state is not.
_FIT_THREADS: Final = 1

ARTIFACT_MODEL_FILE: Final = "model.joblib"
ARTIFACT_CARD_FILE: Final = "card.json"


class ProvenanceError(RuntimeError):
    """Raised when a model would be used against data of a kind it was not trained on."""


@dataclass(frozen=True, slots=True)
class ModelCard:
    """What a fitted artifact is, and what it is allowed to be used for.

    Everything here is an *input* to the fit, never a result. That is M6's ``_input_digest``
    reasoning applied to a second irreproducible component: hashing the fitted bytes would report a
    change whenever a library patch shifted a float, while hashing the inputs answers the question
    actually worth asking of a divergent run -- *was it given the same problem?*
    """

    model_key: str
    target: str
    site: str
    training_data_source: str
    selection_rule_id: str
    telemetry_lag_hours: int
    train_start: str
    train_end: str
    feature_names: tuple[str, ...]
    hyperparameters: Mapping[str, Any]
    quantiles: tuple[float, ...]
    library_versions: Mapping[str, str]
    seed: int
    # Fold geometry. Two runs that differ only here produce different training sets and different
    # predictions; without them in the hash `derived.forecast_runs` cannot tell the experiments
    # apart, which is the one question the hash exists to answer.
    min_train_days: int = 0
    fold_stride_days: int = 0
    n_train_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "target": self.target,
            "site": self.site,
            "training_data_source": self.training_data_source,
            "selection_rule_id": self.selection_rule_id,
            "telemetry_lag_hours": self.telemetry_lag_hours,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "feature_names": list(self.feature_names),
            "hyperparameters": dict(self.hyperparameters),
            "quantiles": list(self.quantiles),
            "library_versions": dict(self.library_versions),
            "seed": self.seed,
            "min_train_days": self.min_train_days,
            "fold_stride_days": self.fold_stride_days,
            "n_train_rows": self.n_train_rows,
        }

    @property
    def config_hash(self) -> str:
        """A digest over the fit's inputs. Excludes ``n_train_rows``, which is an outcome.

        Sorted keys and compact separators, matching ``ingest.content_hash`` and
        ``dispatch.store._input_digest`` -- three hashes in this repo, one serialisation policy.
        """
        payload = {k: v for k, v in self.to_dict().items() if k != "n_train_rows"}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def library_versions() -> dict[str, str]:
    """The versions a divergence can be attributed to. Read once, recorded per run.

    pvlib and pandas are in here because ``pvlib_physical`` and all four solar-geometry features are
    pure pvlib output (over a pandas index): a pvlib upgrade moves ``mart_forecast_eval`` numbers,
    and a provenance record that could not name the cause would be recording the wrong things.
    """
    import pandas
    import pvlib
    import sklearn

    return {
        "numpy": np.__version__,
        "pandas": pandas.__version__,
        "pvlib": pvlib.__version__,
        "scikit-learn": sklearn.__version__,
        "joblib": joblib.__version__,
    }


@dataclass(slots=True)
class QuantileForecaster:
    """One model per quantile, fitted and predicted together.

    Three independent fits rather than one multi-output model, because that is what quantile loss
    is: three different objectives. They can and do cross at the tails on small data -- p10 above
    p50 for some hour -- so :meth:`predict` sorts each row. Sorting is a presentation fix and is
    said so: it makes the intervals readable without pretending the tails are well estimated, which
    at ~130 effective rows in the p10 fit they are not.
    """

    card: ModelCard
    models: dict[float, HistGradientBoostingRegressor] = field(default_factory=dict)

    def fit(self, features: NDArray[np.float64], target: NDArray[np.float64]) -> QuantileForecaster:
        with threadpool_limits(limits=_FIT_THREADS):
            for quantile in self.card.quantiles:
                model = HistGradientBoostingRegressor(
                    loss="quantile", quantile=quantile, **HYPERPARAMETERS
                )
                model.fit(features, target)
                self.models[quantile] = model
        return self

    def predict(self, features: NDArray[np.float64]) -> NDArray[np.float64]:
        """``(n_rows, n_quantiles)``, each row sorted ascending."""
        with threadpool_limits(limits=_FIT_THREADS):
            columns = [self.models[q].predict(features) for q in self.card.quantiles]
        return np.sort(np.column_stack(columns), axis=1)


def fit_quantile_forecaster(
    features: NDArray[np.float64], target: NDArray[np.float64], card: ModelCard
) -> QuantileForecaster:
    """Fit, refusing an empty or all-missing training set rather than producing a constant."""
    usable = np.isfinite(target)
    if not usable.any():
        raise ValueError(
            f"{card.model_key}: no finite target values in the training window "
            f"{card.train_start}..{card.train_end}; nothing to fit"
        )
    return QuantileForecaster(card=card).fit(features[usable], target[usable])


def artifact_dir(config: ForecastConfig, card: ModelCard) -> Path:
    """Where a fitted model lives: gitignored, keyed by the hash of its own inputs.

    CI retrains from scratch every run -- seconds at this data size -- so no binary ever enters the
    repo and there is no pickle-compatibility trap waiting for a scikit-learn upgrade.
    """
    return Path(config.artifact_dir) / card.model_key / card.config_hash


def save_artifact(forecaster: QuantileForecaster, config: ForecastConfig) -> Path:
    """Persist the fitted models and their card side by side."""
    destination = artifact_dir(config, forecaster.card)
    destination.mkdir(parents=True, exist_ok=True)
    joblib.dump(forecaster.models, destination / ARTIFACT_MODEL_FILE)
    (destination / ARTIFACT_CARD_FILE).write_text(
        json.dumps(forecaster.card.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


def load_artifact(directory: Path, *, expected_data_source: str) -> QuantileForecaster:
    """Load a fitted model, refusing one trained on a different kind of reality.

    This is the interlock, and it is deliberately not a warning. A model fitted on synthetic windows
    has learned the simulator's physics -- including, for the PV residual, how to undo a
    transposition that a real tilted array genuinely needs. Loading it to predict a real house would
    apply a confident correction in the wrong direction, and nothing downstream could tell.
    """
    card_payload = json.loads((directory / ARTIFACT_CARD_FILE).read_text(encoding="utf-8"))
    trained_on = card_payload["training_data_source"]
    if trained_on != expected_data_source:
        raise ProvenanceError(
            f"refusing to load a {trained_on}-trained model for {expected_data_source} prediction "
            f"({directory}). A model fitted on simulated physics has learned corrections that do "
            f"not transfer; retrain against {expected_data_source} data instead."
        )
    card = ModelCard(
        model_key=card_payload["model_key"],
        target=card_payload["target"],
        site=card_payload["site"],
        training_data_source=trained_on,
        selection_rule_id=card_payload["selection_rule_id"],
        telemetry_lag_hours=card_payload["telemetry_lag_hours"],
        train_start=card_payload["train_start"],
        train_end=card_payload["train_end"],
        feature_names=tuple(card_payload["feature_names"]),
        hyperparameters=card_payload["hyperparameters"],
        quantiles=tuple(card_payload["quantiles"]),
        library_versions=card_payload["library_versions"],
        seed=card_payload["seed"],
        min_train_days=card_payload.get("min_train_days", 0),
        fold_stride_days=card_payload.get("fold_stride_days", 0),
        n_train_rows=card_payload.get("n_train_rows", 0),
    )
    forecaster = QuantileForecaster(card=card)
    forecaster.models = joblib.load(directory / ARTIFACT_MODEL_FILE)
    return forecaster


def pinball_loss(actual: float, predicted: float, quantile: float) -> float:
    """The quantile (pinball) loss for one observation.

    Written here and mirrored in ``mart_forecast_eval``; ``tests/dbt/test_forecast_reconciliation``
    asserts the two agree row by row, exactly as the tariff and dispatch reconciliations do.
    """
    delta = actual - predicted
    return quantile * delta if delta >= 0 else (quantile - 1.0) * delta


def irreducible_load_mae_kwh(mean_hourly_load_kwh: float, noise_amplitude: float) -> float:
    """The MAE floor M3's load generator imposes, in closed form.

    Synthetic load is ``shape * (1 + U(-a, a))``, so the expected absolute error of a *perfect*
    predictor of the shape is ``E|U(-a, a)| = a/2`` times the mean level. Reporting this beside the
    models turns "the GBM crushes persistence" from a suspicious result into a bounded one: nothing
    can do better than this on seeded data, and a model that appears to is reading something it
    should not be.
    """
    return mean_hourly_load_kwh * noise_amplitude / 2.0
