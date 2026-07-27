"""Guard: no model reads the forecast source without carrying the vintage dimension.

The no-lookahead invariant promised in M2 -- forecasts are keyed by (issue_time, target_time) and
nothing selects a forecast value without an issue-time predicate -- is enforced *structurally*
over the compiled dbt manifest, not by scanning SQL text (a transitive dependent inherits
``issue_time`` from the staging model and would fool a substring scan).

* **Layer 1** -- exactly one model may depend *directly* on ``raw.forecast_observations``. A source
  id appears in a model's ``depends_on.nodes`` only for a direct ``{{ source(...) }}`` reference;
  transitive dependents list ``stg_weather_forecast`` instead. So the set of direct readers must be
  exactly ``{stg_weather_forecast}``.
* **Layer 2** -- ``stg_weather_forecast`` must still carry an ``issue_time`` / ``issue_date``
  predicate, so the one permitted reader cannot be silently gutted.

Skipped when the manifest is absent (dbt not built), so the general ``pytest`` run stays green; CI
runs ``dbt build`` first, sets ``ENERGY_REQUIRE_DBT=1``, and that skip becomes a failure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.dbt.warehouse_guard import skip_or_fail

MANIFEST = Path(__file__).resolve().parents[2] / "dbt" / "target" / "manifest.json"
FORECAST_SOURCE_ID = "source.energy_platform.raw.forecast_observations"
ALLOWED_DIRECT_READER = "model.energy_platform.stg_weather_forecast"
VINTAGE_PREDICATE = re.compile(r"\bissue_time\b|\bissue_date\b")


def _load_manifest() -> dict[str, Any]:
    if not MANIFEST.exists():
        skip_or_fail(f"dbt manifest not found at {MANIFEST}; run `dbt build` first")
    parsed: dict[str, Any] = json.loads(MANIFEST.read_text())
    return parsed


def test_only_vintage_staging_reads_forecast_source() -> None:
    """Exactly one model may read raw.forecast_observations directly (the vintage staging)."""
    nodes = _load_manifest()["nodes"]
    direct_readers = {
        uid
        for uid, node in nodes.items()
        if node.get("resource_type") == "model"
        and FORECAST_SOURCE_ID in node.get("depends_on", {}).get("nodes", [])
    }
    assert direct_readers == {ALLOWED_DIRECT_READER}, (
        "no-lookahead guard: exactly one model (the vintage staging) may select from "
        f"raw.forecast_observations; found direct readers {sorted(direct_readers)}"
    )


def test_vintage_staging_carries_issue_predicate() -> None:
    """The one permitted reader must still reference the issue-time vintage dimension."""
    node = _load_manifest()["nodes"].get(ALLOWED_DIRECT_READER)
    assert node is not None, f"{ALLOWED_DIRECT_READER} missing from manifest"
    sql = node.get("compiled_code") or node.get("raw_code") or ""
    assert VINTAGE_PREDICATE.search(sql), (
        "stg_weather_forecast must reference issue_time/issue_date -- the vintage dimension that "
        "keeps the no-lookahead guard meaningful"
    )
